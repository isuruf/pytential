# -*- coding: utf-8 -*-
from __future__ import division, absolute_import

__copyright__ = "Copyright (C) 2013 Andreas Kloeckner"

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

import numpy as np
from pytools import memoize_method
from meshmode.discretization import Discretization
from pytential.qbx.target_assoc import QBXTargetAssociationFailedException
from pytential.source import LayerPotentialSourceBase

import pyopencl as cl

import logging
logger = logging.getLogger(__name__)


__doc__ = """
.. autoclass:: QBXLayerPotentialSource

.. autoclass:: QBXTargetAssociationFailedException
"""


# {{{ QBX layer potential source

class _not_provided:  # noqa: N801
    pass


class QBXLayerPotentialSource(LayerPotentialSourceBase):
    """A source discretization for a QBX layer potential.

    .. attribute :: qbx_order
    .. attribute :: fmm_order

    .. automethod :: __init__
    .. automethod :: with_refinement
    .. automethod :: copy

    .. attribute :: stage2_density_discr
    .. attribute :: quad_stage2_density_discr

    See :ref:`qbxguts` for some information on the inner workings of this.
    """

    # {{{ constructor / copy

    def __init__(self,
            density_discr,
            fine_order,
            qbx_order=None,
            fmm_order=None,
            fmm_level_to_order=None,
            to_refined_connection=None,
            expansion_factory=None,
            target_association_tolerance=_not_provided,

            # begin experimental arguments
            # FIXME default debug=False once everything has matured
            debug=True,
            _refined_for_global_qbx=False,
            _expansions_in_tree_have_extent=True,
            _expansion_stick_out_factor=0.5,
            _well_sep_is_n_away=2,
            _max_leaf_refine_weight=None,
            _box_extent_norm=None,
            _from_sep_smaller_crit=None,
            _from_sep_smaller_min_nsources_cumul=None,
            _tree_kind="adaptive",
            _use_target_specific_qbx=None,
            geometry_data_inspector=None,
            cost_model=None,
            fmm_backend="sumpy",
            use_fft=False,
            target_stick_out_factor=_not_provided):
        """
        :arg fine_order: The total degree to which the (upsampled)
             underlying quadrature is exact.
        :arg to_refined_connection: A connection used for resampling from
             *density_discr* the fine density discretization.  It is assumed
             that the fine density discretization given by
             *to_refined_connection.to_discr* is *not* already upsampled. May
             be *None*.
        :arg fmm_order: `False` for direct calculation. May not be given if
            *fmm_level_to_order* is given.
        :arg fmm_level_to_order: A function that takes arguments of
             *(kernel, kernel_args, tree, level)* and returns the expansion
             order to be used on a given *level* of *tree* with *kernel*, where
             *kernel* is the :class:`sumpy.kernel.Kernel` being evaluated, and
             *kernel_args* is a set of *(key, value)* tuples with evaluated
             kernel arguments. May not be given if *fmm_order* is given.

        Experimental arguments without a promise of forward compatibility:

        :arg _use_target_specific_qbx: Whether to use target-specific
            acceleration by default if possible. *None* means
            "use if possible".
        :arg cost_model: Either *None* or instance of
             :class:`~pytential.qbx.cost.CostModel`, used for gathering modeled
             costs (experimental)
        :arg use_fft: A boolean to choose whether an FFT is used for multipole
             to local translation. Used only when fmm_backend is sumpy.
        """

        # {{{ argument processing

        if fine_order is None:
            raise ValueError("fine_order must be provided.")

        if qbx_order is None:
            raise ValueError("qbx_order must be provided.")

        if target_stick_out_factor is not _not_provided:
            from warnings import warn
            warn("target_stick_out_factor has been renamed to "
                    "target_association_tolerance. "
                    "Using target_stick_out_factor is deprecated "
                    "and will stop working in 2018.",
                    DeprecationWarning, stacklevel=2)

            if target_association_tolerance is not _not_provided:
                raise TypeError("May not pass both target_association_tolerance and "
                        "target_stick_out_factor.")

            target_association_tolerance = target_stick_out_factor

        del target_stick_out_factor

        if target_association_tolerance is _not_provided:
            target_association_tolerance = float(
                    np.finfo(density_discr.real_dtype).eps) * 1e3

        if fmm_order is not None and fmm_level_to_order is not None:
            raise TypeError("may not specify both fmm_order and fmm_level_to_order")

        if _box_extent_norm is None:
            _box_extent_norm = "l2"

        if _from_sep_smaller_crit is None:
            # This seems to win no matter what the box extent norm is
            # https://gitlab.tiker.net/papers/2017-qbx-fmm-3d/issues/10
            _from_sep_smaller_crit = "precise_linf"

        if fmm_level_to_order is None:
            if fmm_order is False:
                fmm_level_to_order = False
            else:
                def fmm_level_to_order(kernel, kernel_args, tree, level):  # noqa pylint:disable=function-redefined
                    return fmm_order

        if _max_leaf_refine_weight is None:
            if density_discr.ambient_dim == 2:
                # FIXME: This should be verified now that l^2 is the default.
                _max_leaf_refine_weight = 64
            elif density_discr.ambient_dim == 3:
                # For static_linf/linf: https://gitlab.tiker.net/papers/2017-qbx-fmm-3d/issues/8#note_25009  # noqa
                # For static_l2/l2: https://gitlab.tiker.net/papers/2017-qbx-fmm-3d/issues/12  # noqa
                _max_leaf_refine_weight = 512
            else:
                # Just guessing...
                _max_leaf_refine_weight = 64

        if _from_sep_smaller_min_nsources_cumul is None:
            # See here for the comment thread that led to these defaults:
            # https://gitlab.tiker.net/inducer/boxtree/merge_requests/28#note_18661
            if density_discr.dim == 1:
                _from_sep_smaller_min_nsources_cumul = 15
            else:
                _from_sep_smaller_min_nsources_cumul = 30

        # }}}

        LayerPotentialSourceBase.__init__(self, density_discr)

        self.fine_order = fine_order
        self.qbx_order = qbx_order
        self.fmm_level_to_order = fmm_level_to_order

        assert target_association_tolerance is not None

        self.target_association_tolerance = target_association_tolerance
        self.fmm_backend = fmm_backend
        self.use_fft = use_fft

        # Default values are lazily provided if these are None
        self._to_refined_connection = to_refined_connection

        if expansion_factory is None:
            from sumpy.expansion import DefaultExpansionFactory
            expansion_factory = DefaultExpansionFactory()
        self.expansion_factory = expansion_factory

        self.debug = debug
        self._refined_for_global_qbx = _refined_for_global_qbx
        self._expansions_in_tree_have_extent = \
                _expansions_in_tree_have_extent
        self._expansion_stick_out_factor = _expansion_stick_out_factor
        self._well_sep_is_n_away = _well_sep_is_n_away
        self._max_leaf_refine_weight = _max_leaf_refine_weight
        self._box_extent_norm = _box_extent_norm
        self._from_sep_smaller_crit = _from_sep_smaller_crit
        self._from_sep_smaller_min_nsources_cumul = \
                _from_sep_smaller_min_nsources_cumul
        self._tree_kind = _tree_kind
        self._use_target_specific_qbx = _use_target_specific_qbx
        self.geometry_data_inspector = geometry_data_inspector

        if cost_model is None:
            from pytential.qbx.cost import CostModel
            cost_model = CostModel()

        self.cost_model = cost_model

        # /!\ *All* parameters set here must also be set by copy() below,
        # otherwise they will be reset to their default values behind your
        # back if the layer potential source is ever copied. (such as
        # during refinement)

    def copy(
            self,
            density_discr=None,
            fine_order=None,
            qbx_order=None,
            fmm_order=_not_provided,
            fmm_level_to_order=_not_provided,
            to_refined_connection=None,
            expansion_factory=None,
            target_association_tolerance=_not_provided,
            _expansions_in_tree_have_extent=_not_provided,
            _expansion_stick_out_factor=_not_provided,
            _max_leaf_refine_weight=None,
            _box_extent_norm=None,
            _from_sep_smaller_crit=None,
            _tree_kind=None,
            _use_target_specific_qbx=_not_provided,
            geometry_data_inspector=None,
            cost_model=_not_provided,
            fmm_backend=None,
            use_fft=None,

            debug=_not_provided,
            _refined_for_global_qbx=_not_provided,
            target_stick_out_factor=_not_provided,
            ):

        # {{{ argument processing

        if target_stick_out_factor is not _not_provided:
            from warnings import warn
            warn("target_stick_out_factor has been renamed to "
                    "target_association_tolerance. "
                    "Using target_stick_out_factor is deprecated "
                    "and will stop working in 2018.",
                    DeprecationWarning, stacklevel=2)

            if target_association_tolerance is not _not_provided:
                raise TypeError("May not pass both target_association_tolerance and "
                        "target_stick_out_factor.")

            target_association_tolerance = target_stick_out_factor

        elif target_association_tolerance is _not_provided:
            target_association_tolerance = self.target_association_tolerance

        del target_stick_out_factor

        # }}}

        kwargs = {}

        if (fmm_order is not _not_provided
                and fmm_level_to_order is not _not_provided):
            raise TypeError("may not specify both fmm_order and fmm_level_to_order")
        elif fmm_order is not _not_provided:
            kwargs["fmm_order"] = fmm_order
        elif fmm_level_to_order is not _not_provided:
            kwargs["fmm_level_to_order"] = fmm_level_to_order
        else:
            kwargs["fmm_level_to_order"] = self.fmm_level_to_order

        # FIXME Could/should share wrangler and geometry kernels
        # if no relevant changes have been made.
        return QBXLayerPotentialSource(
                density_discr=density_discr or self.density_discr,
                fine_order=(
                    fine_order if fine_order is not None else self.fine_order),
                qbx_order=qbx_order if qbx_order is not None else self.qbx_order,

                target_association_tolerance=target_association_tolerance,
                to_refined_connection=(
                    to_refined_connection or self._to_refined_connection),
                expansion_factory=(
                    expansion_factory or self.expansion_factory),

                debug=(
                    # False is a valid value here
                    debug if debug is not _not_provided else self.debug),
                _refined_for_global_qbx=(
                    # False is a valid value here
                    _refined_for_global_qbx
                    if _refined_for_global_qbx is not _not_provided
                    else self._refined_for_global_qbx),
                _expansions_in_tree_have_extent=(
                    # False is a valid value here
                    _expansions_in_tree_have_extent
                    if _expansions_in_tree_have_extent is not _not_provided
                    else self._expansions_in_tree_have_extent),
                _expansion_stick_out_factor=(
                    # 0 is a valid value here
                    _expansion_stick_out_factor
                    if _expansion_stick_out_factor is not _not_provided
                    else self._expansion_stick_out_factor),
                _well_sep_is_n_away=self._well_sep_is_n_away,
                _max_leaf_refine_weight=(
                    _max_leaf_refine_weight or self._max_leaf_refine_weight),
                _box_extent_norm=(_box_extent_norm or self._box_extent_norm),
                _from_sep_smaller_crit=(
                    _from_sep_smaller_crit or self._from_sep_smaller_crit),
                _from_sep_smaller_min_nsources_cumul=(
                    self._from_sep_smaller_min_nsources_cumul),
                _tree_kind=_tree_kind or self._tree_kind,
                _use_target_specific_qbx=(_use_target_specific_qbx
                    if _use_target_specific_qbx is not _not_provided
                    else self._use_target_specific_qbx),
                geometry_data_inspector=(
                    geometry_data_inspector or self.geometry_data_inspector),
                cost_model=(
                    # None is a valid value here
                    cost_model
                    if cost_model is not _not_provided
                    else self.cost_model),
                fmm_backend=fmm_backend or self.fmm_backend,
                use_fft=use_fft or self.use_fft,
                **kwargs)

    # }}}

    @property
    def stage2_density_discr(self):
        """The refined, interpolation-focused density discretization (no oversampling).
        """
        return (self._to_refined_connection.to_discr
                if self._to_refined_connection is not None
                else self.density_discr)

    @property
    @memoize_method
    def refined_interp_to_ovsmp_quad_connection(self):
        from meshmode.discretization.connection import make_same_mesh_connection

        return make_same_mesh_connection(
                self.quad_stage2_density_discr,
                self.stage2_density_discr)

    @property
    @memoize_method
    def quad_stage2_density_discr(self):
        """The refined, quadrature-focused density discretization (with upsampling).
        """
        from meshmode.discretization.poly_element import (
                QuadratureSimplexGroupFactory)

        return Discretization(
            self.density_discr.cl_context, self.stage2_density_discr.mesh,
            QuadratureSimplexGroupFactory(self.fine_order),
            self.real_dtype)

    # {{{ weights and area elements

    @memoize_method
    def weights_and_area_elements(self):
        from pytential import bind, sym
        with cl.CommandQueue(self.cl_context) as queue:
            return bind(self, sym.weights_and_area_elements(
                self.ambient_dim,
                dofdesc=sym.QBX_SOURCE_QUAD_STAGE2))(queue).with_queue(None)

    # }}}

    @property
    @memoize_method
    def resampler(self):
        from meshmode.discretization.connection import \
                ChainedDiscretizationConnection

        conn = self.refined_interp_to_ovsmp_quad_connection

        if self._to_refined_connection is not None:
            return ChainedDiscretizationConnection(
                    [self._to_refined_connection, conn])

        return conn

    @property
    @memoize_method
    def direct_resampler(self):
        """
        .. warning::

            This always returns a
            :class:`~meshmode.discretization.connection.DirectDiscretizationConnection`.
            In case the geometry has been refined multiple times, a direct
            connection can have a large number of groups and/or
            interpolation batches, making it scale significantly worse than
            the one returned by :attr:`resampler`.
        """
        from meshmode.discretization.connection import \
                flatten_chained_connection

        conn = self.resampler
        with cl.CommandQueue(self.cl_context) as queue:
            conn = flatten_chained_connection(queue, conn)

        return conn

    @property
    @memoize_method
    def tree_code_container(self):
        from pytential.qbx.utils import TreeCodeContainer
        return TreeCodeContainer(self.cl_context)

    @property
    @memoize_method
    def refiner_code_container(self):
        from pytential.qbx.refinement import RefinerCodeContainer
        return RefinerCodeContainer(self.cl_context, self.tree_code_container)

    @property
    @memoize_method
    def target_association_code_container(self):
        from pytential.qbx.target_assoc import TargetAssociationCodeContainer
        return TargetAssociationCodeContainer(
                self.cl_context, self.tree_code_container)

    @memoize_method
    def with_refinement(self, target_order=None, kernel_length_scale=None,
            maxiter=None, visualize=False, refiner=None,
            _expansion_disturbance_tolerance=None,
            _force_stage2_uniform_refinement_rounds=None,
            _scaled_max_curvature_threshold=None):
        """
        :arg refiner: If the mesh underlying :attr:`density_discr`
            is itself the result of refinement, then its
            :class:`meshmode.refinement.Refiner` instance may need to
            be reused for continued refinement. This argument
            provides the opportunity to pass in an existing refiner
            that should be used for continued refinement.
        :returns: a tuple ``(lpot_src, cnx)``, where ``lpot_src`` is a
            :class:`QBXLayerPotentialSource` and ``cnx`` is a
            :class:`meshmode.discretization.connection.DiscretizationConnection`
            from the originally given to the refined geometry.
        """
        from pytential.qbx.refinement import refine_for_global_qbx

        from meshmode.discretization.poly_element import (
                InterpolatoryQuadratureSimplexGroupFactory)

        if target_order is None:
            target_order = self.density_discr.groups[0].order

        with cl.CommandQueue(self.cl_context) as queue:
            lpot, connection = refine_for_global_qbx(
                    self,
                    self.refiner_code_container.get_wrangler(queue),
                    InterpolatoryQuadratureSimplexGroupFactory(target_order),
                    kernel_length_scale=kernel_length_scale,
                    maxiter=maxiter, visualize=visualize,
                    expansion_disturbance_tolerance=_expansion_disturbance_tolerance,
                    force_stage2_uniform_refinement_rounds=(
                        _force_stage2_uniform_refinement_rounds),
                    scaled_max_curvature_threshold=(
                        _scaled_max_curvature_threshold),
                    refiner=refiner)

        return lpot, connection

    # {{{ internal API

    @memoize_method
    def qbx_fmm_geometry_data(self, target_discrs_and_qbx_sides):
        """
        :arg target_discrs_and_qbx_sides:
            a tuple of *(discr, qbx_forced_limit)*
            tuples, where *discr* is a
            :class:`meshmode.discretization.Discretization`
            or
            :class:`pytential.target.TargetBase`
            instance
        """
        from pytential.qbx.geometry import QBXFMMGeometryData

        return QBXFMMGeometryData(self.qbx_fmm_code_getter,
                self, target_discrs_and_qbx_sides,
                target_association_tolerance=self.target_association_tolerance,
                tree_kind=self._tree_kind,
                debug=self.debug)

    # }}}

    # {{{ helpers for symbolic operator processing

    def preprocess_optemplate(self, name, discretizations, expr):
        """
        :arg name: The symbolic name for *self*, which the preprocessor
            should use to find which expressions it is allowed to modify.
        """
        from pytential.symbolic.mappers import QBXPreprocessor
        return QBXPreprocessor(name, discretizations)(expr)

    def op_group_features(self, expr):
        from sumpy.kernel import AxisTargetDerivativeRemover
        result = (
                expr.source, expr.density,
                AxisTargetDerivativeRemover()(expr.kernel),
                )

        return result

    # }}}

    # {{{ internal functionality for execution

    def exec_compute_potential_insn(self, queue, insn, bound_expr, evaluate,
            return_timing_data):
        extra_args = {}

        if self.fmm_level_to_order is False:
            func = self.exec_compute_potential_insn_direct
            extra_args["return_timing_data"] = return_timing_data

        else:
            func = self.exec_compute_potential_insn_fmm

            def drive_fmm(wrangler, strengths, geo_data, kernel, kernel_arguments):
                del geo_data, kernel, kernel_arguments
                from pytential.qbx.fmm import drive_fmm
                if return_timing_data:
                    timing_data = {}
                else:
                    timing_data = None
                return drive_fmm(wrangler, strengths, timing_data), timing_data

            extra_args["fmm_driver"] = drive_fmm

        return self._dispatch_compute_potential_insn(
                queue, insn, bound_expr, evaluate, func, extra_args)

    def cost_model_compute_potential_insn(self, queue, insn, bound_expr, evaluate):
        """Using :attr:`cost_model`, evaluate the cost of executing *insn*.
        Cost model results are gathered in
        :attr:`pytential.symbolic.execution.BoundExpression.modeled_cost`
        along the way.

        :returns: whatever :meth:`exec_compute_potential_insn_fmm` returns.
        """

        if self.fmm_level_to_order is False:
            raise NotImplementedError("perf modeling direct evaluations")

        def drive_cost_model(
                    wrangler, strengths, geo_data, kernel, kernel_arguments):
            del strengths
            cost_model_result = (
                    self.cost_model(wrangler, geo_data, kernel, kernel_arguments))

            from pytools.obj_array import with_object_array_or_scalar
            output_placeholder = with_object_array_or_scalar(
                wrangler.finalize_potentials,
                wrangler.full_output_zeros()
            )

            return output_placeholder, cost_model_result

        return self._dispatch_compute_potential_insn(
                queue, insn, bound_expr, evaluate,
                self.exec_compute_potential_insn_fmm,
                extra_args={"fmm_driver": drive_cost_model})

    def _dispatch_compute_potential_insn(self, queue, insn, bound_expr,
            evaluate, func, extra_args=None):
        if not self._refined_for_global_qbx:
            from warnings import warn
            warn(
                    "Executing global QBX without refinement. "
                    "This is unlikely to work.")

        if extra_args is None:
            extra_args = {}

        return func(queue, insn, bound_expr, evaluate, **extra_args)

    @property
    @memoize_method
    def qbx_fmm_code_getter(self):
        from pytential.qbx.geometry import QBXFMMGeometryCodeGetter
        return QBXFMMGeometryCodeGetter(self.cl_context, self.ambient_dim,
                self.tree_code_container, debug=self.debug,
                _well_sep_is_n_away=self._well_sep_is_n_away,
                _from_sep_smaller_crit=self._from_sep_smaller_crit)

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
        qbx_local_factory = partial(local_expn_class, fmm_kernel)

        if self.fmm_backend == "sumpy":
            from pytential.qbx.fmm import \
                    QBXSumpyExpansionWranglerCodeContainer
            return QBXSumpyExpansionWranglerCodeContainer(
                    self.cl_context,
                    fmm_mpole_factory, fmm_local_factory, qbx_local_factory,
                    out_kernels, use_fft=self.use_fft)

        elif self.fmm_backend == "fmmlib":
            from pytential.qbx.fmmlib import \
                    QBXFMMLibExpansionWranglerCodeContainer
            return QBXFMMLibExpansionWranglerCodeContainer(
                    self.cl_context,
                    fmm_mpole_factory, fmm_local_factory, qbx_local_factory,
                    out_kernels)

        else:
            raise ValueError("invalid FMM backend: %s" % self.fmm_backend)

    def get_target_discrs_and_qbx_sides(self, insn, bound_expr):
        """Build the list of unique target discretizations used by the
        provided instruction.
        """
        # map (name, qbx_side) to number in list
        target_name_and_side_to_number = {}
        # list of tuples (discr, qbx_side)
        target_discrs_and_qbx_sides = []

        for o in insn.outputs:
            key = (o.target_name, o.qbx_forced_limit)
            if key not in target_name_and_side_to_number:
                target_name_and_side_to_number[key] = \
                        len(target_discrs_and_qbx_sides)

                target_discr = bound_expr.places.get_geometry(o.target_name)
                if isinstance(target_discr, LayerPotentialSourceBase):
                    target_discr = target_discr.density_discr

                qbx_forced_limit = o.qbx_forced_limit
                if qbx_forced_limit is None:
                    qbx_forced_limit = 0

                target_discrs_and_qbx_sides.append(
                        (target_discr, qbx_forced_limit))

        return target_name_and_side_to_number, tuple(target_discrs_and_qbx_sides)

    def exec_compute_potential_insn_fmm(self, queue, insn, bound_expr, evaluate,
            fmm_driver):
        """
        :arg fmm_driver: A function that accepts four arguments:
            *wrangler*, *strength*, *geo_data*, *kernel*, *kernel_arguments*
        :returns: a tuple ``(assignments, extra_outputs)``, where *assignments*
            is a list of tuples containing pairs ``(name, value)`` representing
            assignments to be performed in the evaluation context.
            *extra_outputs* is data that *fmm_driver* may return
            (such as timing data), passed through unmodified.
        """
        target_name_and_side_to_number, target_discrs_and_qbx_sides = (
                self.get_target_discrs_and_qbx_sides(insn, bound_expr))

        geo_data = self.qbx_fmm_geometry_data(target_discrs_and_qbx_sides)

        # FIXME Exert more positive control over geo_data attribute lifetimes using
        # geo_data.<method>.clear_cache(geo_data).

        # FIXME Synthesize "bad centers" around corners and edges that have
        # inadequate QBX coverage.

        # FIXME don't compute *all* output kernels on all targets--respect that
        # some target discretizations may only be asking for derivatives (e.g.)

        strengths = (evaluate(insn.density).with_queue(queue)
                * self.weights_and_area_elements())
        out_kernels = tuple(knl for knl in insn.kernels)
        fmm_kernel = self.get_fmm_kernel(out_kernels)
        output_and_expansion_dtype = (
                self.get_fmm_output_and_expansion_dtype(fmm_kernel, strengths))
        kernel_extra_kwargs, source_extra_kwargs = (
                self.get_fmm_expansion_wrangler_extra_kwargs(
                    queue, out_kernels, geo_data.tree().user_source_ids,
                    insn.kernel_arguments, evaluate))

        get_wrangler_args = dict(
            queue=queue,
            geo_data=geo_data,
            dtype=output_and_expansion_dtype,
            qbx_order=self.qbx_order,
            fmm_level_to_order=self.fmm_level_to_order,
            source_extra_kwargs=source_extra_kwargs,
            kernel_extra_kwargs=kernel_extra_kwargs,
            _use_target_specific_qbx=self._use_target_specific_qbx
        )

        if self.fmm_backend == "sumpy":
            get_wrangler_args["use_fft"] = self.use_fft

        wrangler = self.expansion_wrangler_code_container(
                fmm_kernel, out_kernels).get_wrangler(**get_wrangler_args)

        from pytential.qbx.geometry import target_state
        if (geo_data.user_target_to_center().with_queue(queue)
                == target_state.FAILED).any().get():
            raise RuntimeError("geometry has failed targets")

        # {{{ geometry data inspection hook

        if self.geometry_data_inspector is not None:
            perform_fmm = self.geometry_data_inspector(insn, bound_expr, geo_data)
            if not perform_fmm:
                return [(o.name, 0) for o in insn.outputs]

        # }}}

        # Execute global QBX.
        all_potentials_on_every_target, extra_outputs = (
                fmm_driver(
                    wrangler, strengths, geo_data, fmm_kernel, kernel_extra_kwargs))

        result = []

        for o in insn.outputs:
            target_side_number = target_name_and_side_to_number[
                    o.target_name, o.qbx_forced_limit]
            target_slice = slice(*geo_data.target_info().target_discr_starts[
                    target_side_number:target_side_number+2])

            result.append((o.name,
                    all_potentials_on_every_target[o.kernel_index][target_slice]))

        return result, extra_outputs

    # }}}

    # {{{ direct execution

    @memoize_method
    def get_lpot_applier(self, kernels):
        # needs to be separate method for caching

        from pytools import any
        if any(knl.is_complex_valued for knl in kernels):
            value_dtype = self.density_discr.complex_dtype
        else:
            value_dtype = self.density_discr.real_dtype

        from sumpy.qbx import LayerPotential
        from sumpy.expansion.local import LineTaylorLocalExpansion
        return LayerPotential(self.cl_context,
                    [LineTaylorLocalExpansion(knl, self.qbx_order)
                        for knl in kernels],
                    value_dtypes=value_dtype)

    @memoize_method
    def get_lpot_applier_on_tgt_subset(self, kernels):
        # needs to be separate method for caching

        from pytools import any
        if any(knl.is_complex_valued for knl in kernels):
            value_dtype = self.density_discr.complex_dtype
        else:
            value_dtype = self.density_discr.real_dtype

        from pytential.qbx.direct import LayerPotentialOnTargetAndCenterSubset
        from sumpy.expansion.local import VolumeTaylorLocalExpansion
        return LayerPotentialOnTargetAndCenterSubset(
                self.cl_context,
                [VolumeTaylorLocalExpansion(knl, self.qbx_order)
                    for knl in kernels],
                value_dtypes=value_dtype)

    @memoize_method
    def get_qbx_target_numberer(self, dtype):
        assert dtype == np.int32
        from pyopencl.scan import GenericScanKernel
        return GenericScanKernel(
                self.cl_context, np.int32,
                arguments="int *tgt_to_qbx_center, int *qbx_tgt_number, int *count",
                input_expr="tgt_to_qbx_center[i] >= 0 ? 1 : 0",
                scan_expr="a+b", neutral="0",
                output_statement="""
                    if (item != prev_item)
                        qbx_tgt_number[item-1] = i;

                    if (i+1 == N)
                        *count = item;
                    """)

    def exec_compute_potential_insn_direct(self, queue, insn, bound_expr, evaluate,
            return_timing_data):
        if return_timing_data:
            from pytential.source import UnableToCollectTimingData
            from warnings import warn
            warn(
                    "Timing data collection not supported.",
                    category=UnableToCollectTimingData)

        lpot_applier = self.get_lpot_applier(insn.kernels)
        p2p = None
        lpot_applier_on_tgt_subset = None

        kernel_args = {}
        for arg_name, arg_expr in six.iteritems(insn.kernel_arguments):
            kernel_args[arg_name] = evaluate(arg_expr)

        strengths = (evaluate(insn.density).with_queue(queue)
                * self.weights_and_area_elements())

        from pytential import bind, sym
        expansion_radii = bind(self,
                sym.expansion_radii(self.ambient_dim))(queue)
        centers = {
                -1: bind(self,
                    sym.expansion_centers(self.ambient_dim, -1))(queue),
                +1: bind(self,
                    sym.expansion_centers(self.ambient_dim, +1))(queue)
                }

        # FIXME: Do this all at once
        result = []
        for o in insn.outputs:
            target_discr = bound_expr.get_discretization(o.target_name)

            is_self = self.density_discr is target_discr

            if is_self:
                # QBXPreprocessor is supposed to have taken care of this
                assert o.qbx_forced_limit is not None
                assert abs(o.qbx_forced_limit) > 0

                evt, output_for_each_kernel = lpot_applier(
                        queue, target_discr.nodes(),
                        self.quad_stage2_density_discr.nodes(),
                        centers[o.qbx_forced_limit],
                        [strengths],
                        expansion_radii=expansion_radii,
                        **kernel_args)
                result.append((o.name, output_for_each_kernel[o.kernel_index]))
            else:
                # no on-disk kernel caching
                if p2p is None:
                    p2p = self.get_p2p(insn.kernels)
                if lpot_applier_on_tgt_subset is None:
                    lpot_applier_on_tgt_subset = self.get_lpot_applier_on_tgt_subset(
                            insn.kernels)

                evt, output_for_each_kernel = p2p(queue,
                        target_discr.nodes(),
                        self.quad_stage2_density_discr.nodes(),
                        [strengths], **kernel_args)

                qbx_forced_limit = o.qbx_forced_limit
                if qbx_forced_limit is None:
                    qbx_forced_limit = 0

                geo_data = self.qbx_fmm_geometry_data(
                        target_discrs_and_qbx_sides=(
                            (target_discr, qbx_forced_limit),
                        ))

                # center-related info is independent of targets

                # First ncenters targets are the centers
                tgt_to_qbx_center = (
                        geo_data.user_target_to_center()[geo_data.ncenters:]
                        .copy(queue=queue)
                        .with_queue(queue))

                qbx_tgt_numberer = self.get_qbx_target_numberer(
                        tgt_to_qbx_center.dtype)
                qbx_tgt_count = cl.array.empty(queue, (), np.int32)
                qbx_tgt_numbers = cl.array.empty_like(tgt_to_qbx_center)

                qbx_tgt_numberer(
                        tgt_to_qbx_center, qbx_tgt_numbers, qbx_tgt_count,
                        queue=queue)

                qbx_tgt_count = int(qbx_tgt_count.get())

                if (o.qbx_forced_limit is not None
                        and abs(o.qbx_forced_limit) == 1
                        and qbx_tgt_count < target_discr.nnodes):
                    raise RuntimeError("Did not find a matching QBX center "
                            "for some targets")

                qbx_tgt_numbers = qbx_tgt_numbers[:qbx_tgt_count]
                qbx_center_numbers = tgt_to_qbx_center[qbx_tgt_numbers]
                qbx_center_numbers.finish()

                tgt_subset_kwargs = kernel_args.copy()
                for i, res_i in enumerate(output_for_each_kernel):
                    tgt_subset_kwargs["result_%d" % i] = res_i

                if qbx_tgt_count:
                    lpot_applier_on_tgt_subset(
                            queue,
                            targets=target_discr.nodes(),
                            sources=self.quad_stage2_density_discr.nodes(),
                            centers=geo_data.centers(),
                            expansion_radii=geo_data.expansion_radii(),
                            strengths=[strengths],
                            qbx_tgt_numbers=qbx_tgt_numbers,
                            qbx_center_numbers=qbx_center_numbers,
                            **tgt_subset_kwargs)

                result.append((o.name, output_for_each_kernel[o.kernel_index]))

        timing_data = {}
        return result, timing_data

    # }}}

    # }}}

# }}}


__all__ = (
        "QBXLayerPotentialSource",
        "QBXTargetAssociationFailedException",
        )

# vim: fdm=marker
