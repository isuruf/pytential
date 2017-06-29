from __future__ import division, absolute_import

__copyright__ = "Copyright (C) 2017 Andreas Kloeckner"

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

import numpy as np
from pytools import memoize_method, Record
import pyopencl as cl  # noqa
import pyopencl.array  # noqa: F401
from boxtree.pyfmmlib_integration import FMMLibExpansionWrangler
from sumpy.kernel import HelmholtzKernel


import logging
logger = logging.getLogger(__name__)


class P2QBXLInfo(Record):
    pass


class QBXFMMLibExpansionWranglerCodeContainer(object):
    def __init__(self, cl_context,
            multipole_expansion_factory, local_expansion_factory,
            qbx_local_expansion_factory, out_kernels):
        self.cl_context = cl_context
        self.multipole_expansion_factory = multipole_expansion_factory
        self.local_expansion_factory = local_expansion_factory
        self.qbx_local_expansion_factory = qbx_local_expansion_factory

        self.out_kernels = out_kernels

    def get_wrangler(self, queue, geo_data, dtype,
            qbx_order, fmm_level_to_order,
            source_extra_kwargs={},
            kernel_extra_kwargs=None):

        return QBXFMMLibExpansionWrangler(self, queue, geo_data, dtype,
                qbx_order, fmm_level_to_order,
                source_extra_kwargs,
                kernel_extra_kwargs)

# }}}


# {{{ host geo data wrapper

class ToHostTransferredGeoDataWrapper(object):
    def __init__(self, queue, geo_data):
        self.queue = queue
        self.geo_data = geo_data

    @memoize_method
    def tree(self):
        return self.traversal().tree

    @memoize_method
    def traversal(self):
        return self.geo_data.traversal().get(queue=self.queue)

    @property
    def ncenters(self):
        return self.geo_data.ncenters

    @memoize_method
    def centers(self):
        return np.array([
            ci.get(queue=self.queue)
            for ci in self.geo_data.centers()])

    @memoize_method
    def global_qbx_centers(self):
        return self.geo_data.global_qbx_centers().get(queue=self.queue)

    @memoize_method
    def qbx_center_to_target_box(self):
        return self.geo_data.qbx_center_to_target_box().get(queue=self.queue)

    @memoize_method
    def non_qbx_box_target_lists(self):
        return self.geo_data.non_qbx_box_target_lists().get(queue=self.queue)

    @memoize_method
    def center_to_tree_targets(self):
        return self.geo_data.center_to_tree_targets().get(queue=self.queue)

    @memoize_method
    def all_targets(self):
        """All (not just non-QBX) targets packaged into a single array."""
        return np.array(list(self.tree().targets))

# }}}


# {{{ fmmlib expansion wrangler

class QBXFMMLibExpansionWrangler(FMMLibExpansionWrangler):
    def __init__(self, code, queue, geo_data, dtype,
            qbx_order, fmm_level_to_order,
            source_extra_kwargs,
            kernel_extra_kwargs):

        self.code = code
        self.queue = queue

        # FMMLib is CPU-only. This wrapper gets the geometry out of
        # OpenCL-land.
        self.geo_data = ToHostTransferredGeoDataWrapper(queue, geo_data)

        self.qbx_order = qbx_order

        # {{{ digest out_kernels

        from sumpy.kernel import AxisTargetDerivative, DirectionalSourceDerivative

        k_names = []
        source_deriv_names = []

        def is_supported_helmknl(knl):
            if isinstance(knl, DirectionalSourceDerivative):
                source_deriv_names.append(knl.dir_vec_name)
                knl = knl.inner_kernel
            else:
                source_deriv_names.append(None)

            result = isinstance(knl, HelmholtzKernel) and knl.dim == 3
            if result:
                k_names.append(knl.helmholtz_k_name)
            return result

        ifgrad = False
        outputs = []
        for out_knl in self.code.out_kernels:
            if is_supported_helmknl(out_knl):
                outputs.append(())
            elif (isinstance(out_knl, AxisTargetDerivative)
                    and is_supported_helmknl(out_knl.inner_kernel)):
                outputs.append((out_knl.axis,))
                ifgrad = True
            else:
                raise NotImplementedError(
                        "only the 3D Helmholtz kernel and its target derivatives "
                        "are supported for now")

        from pytools import is_single_valued
        if not is_single_valued(source_deriv_names):
            raise ValueError("not all kernels passed are the same in"
                    "whether they represent a source derivative")

        source_deriv_name = source_deriv_names[0]
        self.outputs = outputs

        # }}}

        from pytools import single_valued
        k_name = single_valued(k_names)
        helmholtz_k = kernel_extra_kwargs[k_name]

        self.level_orders = [
                fmm_level_to_order(level)
                for level in range(self.geo_data.tree().nlevels)]

        # FIXME: For now
        from pytools import single_valued
        assert single_valued(self.level_orders)

        dipole_vec = None
        if source_deriv_name is not None:
            dipole_vec = np.array([
                    d_i.get(queue=queue)
                    for d_i in source_extra_kwargs[source_deriv_name]],
                    order="F")

        super(QBXFMMLibExpansionWrangler, self).__init__(
                self.geo_data.tree(),

                helmholtz_k=helmholtz_k,
                dipole_vec=dipole_vec,
                dipoles_already_reordered=True,

                # FIXME
                nterms=fmm_level_to_order(0),

                ifgrad=ifgrad)

    # {{{ data vector helpers

    def output_zeros(self):
        """This ought to be called ``non_qbx_output_zeros``, but since
        it has to override the superclass's behavior to integrate seamlessly,
        it needs to be called just :meth:`output_zeros`.
        """

        nqbtl = self.geo_data.non_qbx_box_target_lists()

        from pytools.obj_array import make_obj_array
        return make_obj_array([
                np.zeros(nqbtl.nfiltered_targets, self.dtype)
                for k in self.outputs])

    def full_output_zeros(self):
        """This includes QBX and non-QBX targets."""

        from pytools.obj_array import make_obj_array
        return make_obj_array([
                np.zeros(self.tree.ntargets, self.dtype)
                for k in self.outputs])

    def reorder_sources(self, source_array):
        if isinstance(source_array, cl.array.Array):
            source_array = source_array.get(queue=self.queue)

        return (super(QBXFMMLibExpansionWrangler, self)
                .reorder_sources(source_array))

    def reorder_potentials(self, potentials):
        raise NotImplementedError("reorder_potentials should not "
            "be called on a QBXFMMLibHelmholtzExpansionWrangler")

        # Because this is a multi-stage, more complicated process that combines
        # potentials from non-QBX targets and QBX targets.

    def add_potgrad_onto_output(self, output, output_slice, pot, grad):
        for i_out, out in enumerate(self.outputs):
            if len(out) == 0:
                output[i_out][output_slice] += pot
            elif len(out) == 1:
                axis, = out
                if isinstance(grad, np.ndarray):
                    output[i_out][output_slice] += grad[axis]
                else:
                    assert grad == 0
            else:
                raise ValueError("element '%s' of outputs array not "
                        "understood" % out)

    # }}}

    # {{{ override target lists to only hit non-QBX targets

    def box_target_starts(self):
        nqbtl = self.geo_data.non_qbx_box_target_lists()
        return nqbtl.box_target_starts

    def box_target_counts_nonchild(self):
        nqbtl = self.geo_data.non_qbx_box_target_lists()
        return nqbtl.box_target_counts_nonchild

    def targets(self):
        nqbtl = self.geo_data.non_qbx_box_target_lists()
        return nqbtl.targets

    # }}}

    def qbx_local_expansion_zeros(self):
        return np.zeros(
                    (self.geo_data.ncenters,) + self.expansion_shape(self.qbx_order),
                    dtype=self.dtype)

    # {{{ p2qbxl

    @memoize_method
    def _info_for_form_global_qbx_locals(self):
        logger.info("preparing interaction list for p2qbxl: start")

        geo_data = self.geo_data
        traversal = geo_data.traversal()

        starts = traversal.neighbor_source_boxes_starts
        lists = traversal.neighbor_source_boxes_lists

        qbx_center_to_target_box = geo_data.qbx_center_to_target_box()
        qbx_centers = geo_data.centers()

        center_source_counts = [0]
        for itgt_center, tgt_icenter in enumerate(geo_data.global_qbx_centers()):
            itgt_box = qbx_center_to_target_box[tgt_icenter]

            isrc_box_start = starts[itgt_box]
            isrc_box_stop = starts[itgt_box+1]

            source_count = sum(
                    self.tree.box_source_counts_nonchild[lists[isrc_box]]
                    for isrc_box in range(isrc_box_start, isrc_box_stop))

            center_source_counts.append(source_count)

        center_source_counts = np.array(center_source_counts)
        center_source_starts = np.cumsum(center_source_counts)
        nsources_total = center_source_starts[-1]
        center_source_offsets = np.empty(nsources_total, np.int32)

        isource = 0
        for itgt_center, tgt_icenter in enumerate(geo_data.global_qbx_centers()):
            assert isource == center_source_starts[itgt_center]
            itgt_box = qbx_center_to_target_box[tgt_icenter]

            isrc_box_start = starts[itgt_box]
            isrc_box_stop = starts[itgt_box+1]

            for isrc_box in range(isrc_box_start, isrc_box_stop):
                src_ibox = lists[isrc_box]

                src_pslice = self._get_source_slice(src_ibox)
                ns = self.tree.box_source_counts_nonchild[src_ibox]
                center_source_offsets[isource:isource+ns] = np.arange(
                        src_pslice.start, src_pslice.stop)

                isource += ns

        centers = qbx_centers[:, geo_data.global_qbx_centers()]

        rscale = 1  # FIXME
        rscale_vec = np.empty(len(center_source_counts) - 1, dtype=np.float64)
        rscale_vec.fill(rscale)  # FIXME

        nsources_vec = np.ones(self.tree.nsources, np.int32)

        logger.info("preparing interaction list for p2qbxl: done")

        return P2QBXLInfo(
                centers=centers,
                center_source_starts=center_source_starts,
                center_source_offsets=center_source_offsets,
                nsources_vec=nsources_vec,
                rscale_vec=rscale_vec,
                ngqbx_centers=centers.shape[1],
                )

    def form_global_qbx_locals(self, src_weights):
        geo_data = self.geo_data

        local_exps = self.qbx_local_expansion_zeros()

        if len(geo_data.global_qbx_centers()) == 0:
            return local_exps

        formta_imany = self.get_routine("%ddformta" + self.dp_suffix,
                suffix="_imany")
        info = self._info_for_form_global_qbx_locals()

        kwargs = {}
        kwargs.update(self.kernel_kwargs)

        if self.dipole_vec is None:
            kwargs["charge"] = src_weights
            kwargs["charge_offsets"] = info.center_source_offsets
            kwargs["charge_starts"] = info.center_source_starts

        else:
            kwargs["dipstr"] = src_weights
            kwargs["dipstr_offsets"] = info.center_source_offsets
            kwargs["dipstr_starts"] = info.center_source_starts

            kwargs["dipvec"] = self.dipole_vec
            kwargs["dipvec_offsets"] = info.center_source_offsets
            kwargs["dipvec_starts"] = info.center_source_starts

        # These get max'd/added onto: pass initialized versions.
        ier = np.zeros(info.ngqbx_centers, dtype=np.int32)
        expn = np.zeros(
                (info.ngqbx_centers,) + self.expansion_shape(self.qbx_order),
                dtype=self.dtype)

        ier, expn = formta_imany(
                rscale=info.rscale_vec,

                sources=self._get_single_sources_array(),
                sources_offsets=info.center_source_offsets,
                sources_starts=info.center_source_starts,

                nsources=info.nsources_vec,
                nsources_offsets=info.center_source_offsets,
                nsources_starts=info.center_source_starts,

                center=info.centers,
                nterms=self.nterms,

                ier=ier,
                expn=expn.T,

                **kwargs)

        if np.any(ier != 0):
            raise RuntimeError("formta returned an error")

        local_exps[geo_data.global_qbx_centers()] = expn.T

        return local_exps

    # }}}

    # {{{ m2qbxl

    def translate_box_multipoles_to_qbx_local(self, multipole_exps):
        local_exps = self.qbx_local_expansion_zeros()

        geo_data = self.geo_data
        qbx_center_to_target_box = geo_data.qbx_center_to_target_box()
        qbx_centers = geo_data.centers()
        centers = self.tree.box_centers

        mploc = self.get_translation_routine("%ddmploc", vec_suffix="_imany")

        for isrc_level, ssn in enumerate(
                geo_data.traversal().sep_smaller_by_level):
            source_level_start_ibox, source_mpoles_view = \
                    self.multipole_expansions_view(multipole_exps, isrc_level)

            print("par data prep lev %d" % isrc_level)

            ngqbx_centers = len(geo_data.global_qbx_centers())
            tgt_icenter_vec = geo_data.global_qbx_centers()
            icontaining_tgt_box_vec = qbx_center_to_target_box[tgt_icenter_vec]

            # FIXME
            rscale2 = np.ones(ngqbx_centers, np.float64)

            kwargs = {}
            if self.dim == 3:
                # FIXME Is this right?
                kwargs["radius"] = (
                        np.ones(ngqbx_centers)
                        * self.tree.root_extent * 2**(-isrc_level))

            nsrc_boxes_per_gqbx_center = (
                    ssn.starts[icontaining_tgt_box_vec+1]
                    - ssn.starts[icontaining_tgt_box_vec])
            nsrc_boxes = np.sum(nsrc_boxes_per_gqbx_center)

            src_boxes_starts = np.empty(ngqbx_centers+1, dtype=np.int32)
            src_boxes_starts[0] = 0
            src_boxes_starts[1:] = np.cumsum(nsrc_boxes_per_gqbx_center)

            # FIXME
            rscale1 = np.ones(nsrc_boxes)
            rscale1_offsets = np.arange(nsrc_boxes)

            src_ibox = np.empty(nsrc_boxes, dtype=np.int32)
            for itgt_center, tgt_icenter in enumerate(
                    geo_data.global_qbx_centers()):
                icontaining_tgt_box = qbx_center_to_target_box[tgt_icenter]
                src_ibox[
                        src_boxes_starts[itgt_center]:
                        src_boxes_starts[itgt_center+1]] = (
                    ssn.lists[
                        ssn.starts[icontaining_tgt_box]:
                        ssn.starts[icontaining_tgt_box+1]])

            del itgt_center
            del tgt_icenter
            del icontaining_tgt_box

            print("end par data prep")

            # These get max'd/added onto: pass initialized versions.
            ier = np.zeros(ngqbx_centers, dtype=np.int32)
            expn2 = np.zeros(
                    (ngqbx_centers,) + self.expansion_shape(self.qbx_order),
                    dtype=self.dtype)

            kwargs.update(self.kernel_kwargs)

            expn2 = mploc(
                    rscale1=rscale1,
                    rscale1_offsets=rscale1_offsets,
                    rscale1_starts=src_boxes_starts,

                    center1=centers,
                    center1_offsets=src_ibox,
                    center1_starts=src_boxes_starts,

                    expn1=source_mpoles_view.T,
                    expn1_offsets=src_ibox - source_level_start_ibox,
                    expn1_starts=src_boxes_starts,

                    rscale2=rscale2,
                    # FIXME: center2 has wrong layout, will copy
                    center2=qbx_centers[:, tgt_icenter_vec],
                    expn2=expn2.T,
                    ier=ier,

                    **kwargs).T

            local_exps[geo_data.global_qbx_centers()] += expn2

        return local_exps

    # }}}

    def translate_box_local_to_qbx_local(self, local_exps):
        qbx_expansions = self.qbx_local_expansion_zeros()

        geo_data = self.geo_data
        if geo_data.ncenters == 0:
            return qbx_expansions
        trav = geo_data.traversal()
        qbx_center_to_target_box = geo_data.qbx_center_to_target_box()
        qbx_centers = geo_data.centers()

        rscale = 1  # FIXME

        locloc = self.get_translation_routine("%ddlocloc")

        for isrc_level in range(geo_data.tree().nlevels):
            local_order = self.level_orders[isrc_level]

            lev_box_start, lev_box_stop = self.tree.level_start_box_nrs[
                    isrc_level:isrc_level+2]
            target_level_start_ibox, target_locals_view = \
                    self.local_expansions_view(local_exps, isrc_level)
            assert target_level_start_ibox == lev_box_start

            kwargs = {}
            if self.dim == 3:
                # FIXME Is this right?
                kwargs["radius"] = self.tree.root_extent * 2**(-isrc_level)

            kwargs.update(self.kernel_kwargs)

            for tgt_icenter in range(geo_data.ncenters):
                isrc_box = qbx_center_to_target_box[tgt_icenter]

                tgt_center = qbx_centers[:, tgt_icenter]

                # The box's expansions which we're translating here
                # (our source) is, globally speaking, a target box.

                src_ibox = trav.target_boxes[isrc_box]

                # Is the box number on the level currently under
                # consideration?
                in_range = (lev_box_start <= src_ibox and src_ibox < lev_box_stop)

                if in_range:
                    src_center = self.tree.box_centers[:, src_ibox]
                    tmp_loc_exp = locloc(
                                rscale1=rscale,
                                center1=src_center,
                                expn1=local_exps[src_ibox].T,

                                rscale2=rscale,
                                center2=tgt_center,
                                nterms2=local_order,

                                **kwargs)[..., 0].T

                    qbx_expansions[tgt_icenter] += tmp_loc_exp

        return qbx_expansions

    def eval_qbx_expansions(self, qbx_expansions):
        output = self.full_output_zeros()

        geo_data = self.geo_data
        ctt = geo_data.center_to_tree_targets()
        global_qbx_centers = geo_data.global_qbx_centers()
        qbx_centers = geo_data.centers()

        all_targets = geo_data.all_targets()

        rscale = 1  # FIXME

        taeval = self.get_expn_eval_routine("ta")

        for isrc_center, src_icenter in enumerate(global_qbx_centers):
            for icenter_tgt in range(
                    ctt.starts[src_icenter],
                    ctt.starts[src_icenter+1]):

                center_itgt = ctt.lists[icenter_tgt]

                center = qbx_centers[:, src_icenter]

                pot, grad = taeval(
                        rscale=rscale,
                        center=center,
                        expn=qbx_expansions[src_icenter].T,
                        ztarg=all_targets[:, center_itgt],
                        **self.kernel_kwargs)

                self.add_potgrad_onto_output(output, center_itgt, pot, grad)

        return output

    def finalize_potential(self, potential):
        if self.dim == 3:
            scale_factor = 1/(4*np.pi)
        else:
            raise NotImplementedError(
                    "scale factor for pyfmmlib for %d dimensions" % self.dim)

        return cl.array.to_device(self.queue, potential) * scale_factor

# }}}

# vim: foldmethod=marker
