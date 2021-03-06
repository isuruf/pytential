# -*- coding: utf-8 -*-
from __future__ import division, absolute_import

__copyright__ = """
Copyright (C) 2017 Matt Wala
"""

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import six

from meshmode.array_context import PyOpenCLArrayContext
import numpy as np
import loopy as lp

from boxtree.tools import DeviceDataRecord
from loopy.version import MOST_RECENT_LANGUAGE_VERSION
from pytential.source import LayerPotentialSourceBase
from pytools import memoize_method

import pyopencl as cl
import pyopencl.array  # noqa

import logging
logger = logging.getLogger(__name__)


__doc__ = """
.. autoclass:: UnregularizedLayerPotentialSource
"""


# {{{ (panel-based) unregularized layer potential source

class UnregularizedLayerPotentialSource(LayerPotentialSourceBase):
    """A source discretization for a layer potential discretized with a Nyström
    method that uses panel-based quadrature and does not modify the kernel.

    .. attribute:: fmm_level_to_order
    """

    def __init__(self, density_discr,
            fmm_order=False,
            fmm_level_to_order=None,
            expansion_factory=None,
            # begin undocumented arguments
            # FIXME default debug=False once everything works
            debug=True):
        """
        :arg fmm_order: `False` for direct calculation.
        """
        LayerPotentialSourceBase.__init__(self, density_discr)
        self.debug = debug

        if fmm_order is not False and fmm_level_to_order is not None:
            raise TypeError("may not specify both fmm_order and fmm_level_to_order")

        if fmm_level_to_order is None:
            if fmm_order is not False:
                def fmm_level_to_order(kernel, kernel_args, tree, level):  # noqa pylint:disable=function-redefined
                    return fmm_order
            else:
                fmm_level_to_order = False

        self.density_discr = density_discr
        self.fmm_level_to_order = fmm_level_to_order

        if expansion_factory is None:
            from sumpy.expansion import DefaultExpansionFactory
            expansion_factory = DefaultExpansionFactory()
        self.expansion_factory = expansion_factory

    def copy(
            self,
            density_discr=None,
            fmm_level_to_order=None,
            debug=None,
            ):
        return type(self)(
                fmm_level_to_order=(
                    fmm_level_to_order or self.fmm_level_to_order),
                density_discr=density_discr or self.density_discr,
                debug=debug if debug is not None else self.debug)

    def exec_compute_potential_insn(self, actx: PyOpenCLArrayContext,
            insn, bound_expr, evaluate, return_timing_data):
        if return_timing_data:
            from warnings import warn
            from pytential.source import UnableToCollectTimingData
            warn(
                   "Timing data collection not supported.",
                   category=UnableToCollectTimingData)

        from pytools.obj_array import obj_array_vectorize

        def evaluate_wrapper(expr):
            value = evaluate(expr)
            return obj_array_vectorize(lambda x: x, value)

        if self.fmm_level_to_order is False:
            func = self.exec_compute_potential_insn_direct
        else:
            func = self.exec_compute_potential_insn_fmm

        return func(actx, insn, bound_expr, evaluate_wrapper)

    def op_group_features(self, expr):
        from sumpy.kernel import AxisTargetDerivativeRemover
        result = (
                expr.source, expr.density,
                AxisTargetDerivativeRemover()(expr.kernel),
                )

        return result

    def preprocess_optemplate(self, name, discretizations, expr):
        """
        :arg name: The symbolic name for *self*, which the preprocessor
            should use to find which expressions it is allowed to modify.
        """
        from pytential.symbolic.mappers import UnregularizedPreprocessor
        return UnregularizedPreprocessor(name, discretizations)(expr)

    def exec_compute_potential_insn_direct(self, actx: PyOpenCLArrayContext,
            insn, bound_expr, evaluate):
        kernel_args = {}

        from pytential.utils import flatten_if_needed
        from meshmode.dof_array import flatten, thaw, unflatten

        for arg_name, arg_expr in six.iteritems(insn.kernel_arguments):
            kernel_args[arg_name] = flatten_if_needed(actx, evaluate(arg_expr))

        from pytential import bind, sym
        waa = bind(bound_expr.places, sym.weights_and_area_elements(
            self.ambient_dim, dofdesc=insn.source))(actx)
        strengths = waa * evaluate(insn.density)
        flat_strengths = flatten(strengths)

        results = []
        p2p = None

        for o in insn.outputs:
            target_discr = bound_expr.places.get_discretization(
                    o.target_name.geometry, o.target_name.discr_stage)

            if p2p is None:
                p2p = self.get_p2p(actx, insn.kernels)

            evt, output_for_each_kernel = p2p(actx.queue,
                    flatten_if_needed(actx, target_discr.nodes()),
                    flatten(thaw(actx, self.density_discr.nodes())),
                    [flat_strengths], **kernel_args)

            from meshmode.discretization import Discretization
            result = output_for_each_kernel[o.kernel_index]
            if isinstance(target_discr, Discretization):
                result = unflatten(actx, target_discr, result)

            results.append((o.name, result))

        timing_data = {}
        return results, timing_data

    # {{{ fmm-based execution

    @memoize_method
    def expansion_wrangler_code_container(self, fmm_kernel, out_kernels):
        mpole_expn_class = \
                self.expansion_factory.get_multipole_expansion_class(fmm_kernel)
        local_expn_class = \
                self.expansion_factory.get_local_expansion_class(fmm_kernel)

        from functools import partial
        fmm_mpole_factory = partial(mpole_expn_class, fmm_kernel)
        fmm_local_factory = partial(local_expn_class, fmm_kernel)

        from sumpy.fmm import SumpyExpansionWranglerCodeContainer
        return SumpyExpansionWranglerCodeContainer(
                self.cl_context,
                fmm_mpole_factory,
                fmm_local_factory,
                out_kernels)

    @property
    def fmm_geometry_code_container(self):
        return _FMMGeometryDataCodeContainer(
                self._setup_actx, self.ambient_dim, self.debug)

    def fmm_geometry_data(self, targets):
        return _FMMGeometryData(
                self,
                self.fmm_geometry_code_container,
                targets,
                self.debug)

    def exec_compute_potential_insn_fmm(self, actx: PyOpenCLArrayContext,
            insn, bound_expr, evaluate):
        # {{{ gather unique target discretizations used

        target_name_to_index = {}
        targets = []

        for o in insn.outputs:
            assert o.qbx_forced_limit not in (-1, 1)

            if o.target_name in target_name_to_index:
                continue

            target_name_to_index[o.target_name] = len(targets)
            targets.append(bound_expr.places.get_geometry(o.target_name.geometry))

        targets = tuple(targets)

        # }}}

        # {{{ get wrangler

        geo_data = self.fmm_geometry_data(targets)

        from pytential import bind, sym
        waa = bind(bound_expr.places, sym.weights_and_area_elements(
            self.ambient_dim, dofdesc=insn.source))(actx)
        strengths = waa * evaluate(insn.density)

        from meshmode.dof_array import flatten
        flat_strengths = flatten(strengths)

        out_kernels = tuple(knl for knl in insn.kernels)
        fmm_kernel = self.get_fmm_kernel(out_kernels)
        output_and_expansion_dtype = (
                self.get_fmm_output_and_expansion_dtype(fmm_kernel, strengths))
        kernel_extra_kwargs, source_extra_kwargs = (
                self.get_fmm_expansion_wrangler_extra_kwargs(
                    actx, out_kernels, geo_data.tree().user_source_ids,
                    insn.kernel_arguments, evaluate))

        wrangler = self.expansion_wrangler_code_container(
                fmm_kernel, out_kernels).get_wrangler(
                    actx.queue,
                    geo_data.tree(),
                    output_and_expansion_dtype,
                    self.fmm_level_to_order,
                    source_extra_kwargs=source_extra_kwargs,
                    kernel_extra_kwargs=kernel_extra_kwargs)

        # }}}

        from boxtree.fmm import drive_fmm
        all_potentials_on_every_tgt = drive_fmm(
                geo_data.traversal(), wrangler, flat_strengths,
                timing_data=None)

        # {{{ postprocess fmm

        results = []

        for o in insn.outputs:
            target_index = target_name_to_index[o.target_name]
            target_slice = slice(*geo_data.target_info().target_discr_starts[
                    target_index:target_index+2])
            target_discr = targets[target_index]

            result = all_potentials_on_every_tgt[o.kernel_index][target_slice]

            from meshmode.discretization import Discretization
            if isinstance(target_discr, Discretization):
                from meshmode.dof_array import unflatten
                result = unflatten(actx, target_discr, result)

            results.append((o.name, result))

        # }}}

        timing_data = {}
        return results, timing_data

    # }}}

# }}}


# {{{ fmm tools

class _FMMGeometryDataCodeContainer(object):

    def __init__(self, actx, ambient_dim, debug):
        self.array_context = actx
        self.ambient_dim = ambient_dim
        self.debug = debug

    @property
    def cl_context(self):
        return self.array_context.context

    @memoize_method
    def copy_targets_kernel(self):
        knl = lp.make_kernel(
            """{[dim,i]:
                0<=dim<ndims and
                0<=i<npoints}""",
            """
                targets[dim, i] = points[dim, i]
                """,
            default_offset=lp.auto, name="copy_targets",
            lang_version=MOST_RECENT_LANGUAGE_VERSION)

        knl = lp.fix_parameters(knl, ndims=self.ambient_dim)

        knl = lp.split_iname(knl, "i", 128, inner_tag="l.0", outer_tag="g.0")
        knl = lp.tag_array_axes(knl, "points", "sep, C")

        knl = lp.tag_array_axes(knl, "targets", "stride:auto, stride:1")
        return lp.tag_inames(knl, dict(dim="ilp"))

    @property
    @memoize_method
    def build_tree(self):
        from boxtree import TreeBuilder
        return TreeBuilder(self.cl_context)

    @property
    @memoize_method
    def build_traversal(self):
        from boxtree.traversal import FMMTraversalBuilder
        return FMMTraversalBuilder(self.cl_context)


class _TargetInfo(DeviceDataRecord):
    """
    .. attribute:: targets

        Shape: ``[dim,ntargets]``

    .. attribute:: target_discr_starts

        Shape: ``[ndiscrs+1]``

    .. attribute:: ntargets
    """


class _FMMGeometryData(object):

    def __init__(self, lpot_source, code_getter, target_discrs, debug=True):
        self.lpot_source = lpot_source
        self.code_getter = code_getter
        self.target_discrs = target_discrs
        self.debug = debug

    @property
    def cl_context(self):
        return self.code_getter.cl_context

    @property
    def array_context(self):
        return self.code_getter.array_context

    @property
    def coord_dtype(self):
        return self.lpot_source.density_discr.real_dtype

    @property
    def ambient_dim(self):
        return self.lpot_source.density_discr.ambient_dim

    @memoize_method
    def traversal(self):
        with cl.CommandQueue(self.cl_context) as queue:
            trav, _ = self.code_getter.build_traversal(queue, self.tree(),
                    debug=self.debug)

            return trav

    @memoize_method
    def tree(self):
        """Build and return a :class:`boxtree.tree.Tree`
        for this source with these targets.

        |cached|
        """

        code_getter = self.code_getter
        lpot_src = self.lpot_source
        target_info = self.target_info()

        queue = self.array_context.queue

        nsources = lpot_src.density_discr.ndofs
        nparticles = nsources + target_info.ntargets

        refine_weights = cl.array.zeros(queue, nparticles, dtype=np.int32)
        refine_weights[:nsources] = 1
        refine_weights.finish()

        MAX_LEAF_REFINE_WEIGHT = 32  # noqa

        from meshmode.dof_array import thaw, flatten

        tree, _ = code_getter.build_tree(queue,
                particles=flatten(
                    thaw(self.array_context, lpot_src.density_discr.nodes())),
                targets=target_info.targets,
                max_leaf_refine_weight=MAX_LEAF_REFINE_WEIGHT,
                refine_weights=refine_weights,
                debug=self.debug,
                kind="adaptive")

        return tree

    @memoize_method
    def target_info(self):
        code_getter = self.code_getter
        lpot_src = self.lpot_source
        target_discrs = self.target_discrs

        ntargets = 0
        target_discr_starts = []

        for target_discr in target_discrs:
            target_discr_starts.append(ntargets)
            ntargets += target_discr.ndofs

        target_discr_starts.append(ntargets)

        targets = self.array_context.empty(
                (lpot_src.ambient_dim, ntargets),
                self.coord_dtype)

        from pytential.utils import flatten_if_needed
        for start, target_discr in zip(target_discr_starts, target_discrs):
            code_getter.copy_targets_kernel()(
                    self.array_context.queue,
                    targets=targets[:, start:start+target_discr.ndofs],
                    points=flatten_if_needed(
                        self.array_context, target_discr.nodes()))

        return _TargetInfo(
                targets=targets,
                target_discr_starts=target_discr_starts,
                ntargets=ntargets).with_queue(None)

# }}}


__all__ = (
        "UnregularizedLayerPotentialSource",
        )

# vim: fdm=marker
