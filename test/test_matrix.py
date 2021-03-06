from __future__ import division, absolute_import, print_function

__copyright__ = """
Copyright (C) 2015 Andreas Kloeckner
Copyright (C) 2018 Alexandru Fikl
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

from functools import partial

import numpy as np
import numpy.linalg as la

import pyopencl as cl

from sumpy.symbolic import USE_SYMENGINE
from sumpy.tools import MatrixBlockIndexRanges
from pytools.obj_array import make_obj_array

from pytential import bind, sym
from pytential import GeometryCollection

from meshmode.array_context import PyOpenCLArrayContext
from meshmode.mesh.generation import ellipse, NArmedStarfish

import pytest
from pyopencl.tools import (  # noqa
        pytest_generate_tests_for_pyopencl
        as pytest_generate_tests)

import extra_matrix_data as extra
import logging
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


@pytest.mark.skipif(USE_SYMENGINE,
        reason="https://gitlab.tiker.net/inducer/sumpy/issues/25")
@pytest.mark.parametrize("k", [0, 42])
@pytest.mark.parametrize("curve_fn", [
    partial(ellipse, 3),
    NArmedStarfish(5, 0.25)])
@pytest.mark.parametrize("op_type", ["scalar_mixed", "vector"])
def test_build_matrix(ctx_factory, k, curve_fn, op_type, visualize=False):
    """Checks that the matrix built with `symbolic.execution.build_matrix`
    gives the same (to tolerance) answer as a direct evaluation.
    """

    cl_ctx = ctx_factory()
    queue = cl.CommandQueue(cl_ctx)
    actx = PyOpenCLArrayContext(queue)

    # prevent cache 'splosion
    from sympy.core.cache import clear_cache
    clear_cache()

    case = extra.CurveTestCase(
            name="curve",
            knl_class_or_helmholtz_k=k,
            curve_fn=curve_fn,
            op_type=op_type,
            target_order=7,
            qbx_order=4,
            resolutions=[30])

    logger.info("\n%s", case)

    # {{{ geometry

    qbx = case.get_layer_potential(actx, case.resolutions[-1], case.target_order)

    from pytential.qbx.refinement import refine_geometry_collection
    places = GeometryCollection(qbx, auto_where=case.name)
    places = refine_geometry_collection(places,
            kernel_length_scale=(5 / k if k else None))

    dd = places.auto_source.to_stage1()
    density_discr = places.get_discretization(dd.geometry)

    logger.info("nelements:     %d", density_discr.mesh.nelements)
    logger.info("ndofs:         %d", density_discr.ndofs)

    # }}}

    # {{{ symbolic

    sym_u, sym_op = case.get_operator(places.ambient_dim)
    bound_op = bind(places, sym_op)

    # }}}

    # {{{ dense matrix

    from pytential.symbolic.execution import build_matrix
    mat = actx.to_numpy(
            build_matrix(actx, places, sym_op, sym_u,
            context=case.knl_concrete_kwargs))

    if visualize:
        try:
            import matplotlib.pyplot as pt
        except ImportError:
            visualize = False

    if visualize:
        from sumpy.tools import build_matrix as build_matrix_via_matvec
        mat2 = bound_op.scipy_op(actx, "u", dtype=mat.dtype,
                **case.knl_concrete_kwargs)
        mat2 = build_matrix_via_matvec(mat2)

        logger.info("real %.5e imag %.5e",
                la.norm((mat - mat2).real, "fro") / la.norm(mat2.real, "fro"),
                la.norm((mat - mat2).imag, "fro") / la.norm(mat2.imag, "fro"))

        pt.subplot(121)
        pt.imshow(np.log10(np.abs(1.0e-20 + (mat - mat2).real)))
        pt.colorbar()
        pt.subplot(122)
        pt.imshow(np.log10(np.abs(1.0e-20 + (mat - mat2).imag)))
        pt.colorbar()
        pt.show()
        pt.clf()

    if visualize:
        pt.subplot(121)
        pt.imshow(mat.real)
        pt.colorbar()
        pt.subplot(122)
        pt.imshow(mat.imag)
        pt.colorbar()
        pt.show()
        pt.clf()

    # }}}

    # {{{ check

    from pytential.utils import unflatten_from_numpy, flatten_to_numpy

    np.random.seed(12)
    for i in range(5):
        if isinstance(sym_u, np.ndarray):
            u = make_obj_array([
                np.random.randn(density_discr.ndofs)
                for _ in range(len(sym_u))
                ])
        else:
            u = np.random.randn(density_discr.ndofs)
        u_dev = unflatten_from_numpy(actx, density_discr, u)

        res_matvec = np.hstack(flatten_to_numpy(actx,
            bound_op(actx, u=u_dev, **case.knl_concrete_kwargs)
            ))
        res_mat = mat.dot(np.hstack(u))

        abs_err = la.norm(res_mat - res_matvec, np.inf)
        rel_err = abs_err / la.norm(res_matvec, np.inf)

        logger.info("AbsErr {:.5e} RelErr {:.5e}".format(abs_err, rel_err))
        assert rel_err < 1.0e-13, 'iteration: {}'.format(i)

    # }}}


@pytest.mark.parametrize("ambient_dim", [2, 3])
@pytest.mark.parametrize("block_builder_type", ["qbx", "p2p"])
@pytest.mark.parametrize("index_sparsity_factor", [1.0, 0.6])
@pytest.mark.parametrize("op_type", ["scalar", "scalar_mixed"])
def test_block_builder(ctx_factory, ambient_dim,
        block_builder_type, index_sparsity_factor, op_type, visualize=False):
    """Test that block builders and full matrix builders actually match."""

    ctx = ctx_factory()
    queue = cl.CommandQueue(ctx)
    actx = PyOpenCLArrayContext(queue)

    # prevent cache explosion
    from sympy.core.cache import clear_cache
    clear_cache()

    if ambient_dim == 2:
        case = extra.CurveTestCase(
                name="ellipse",
                target_order=7,
                index_sparsity_factor=index_sparsity_factor,
                op_type=op_type,
                resolutions=[32],
                curve_fn=partial(ellipse, 3.0),
                )
    elif ambient_dim == 3:
        case = extra.TorusTestCase(
                index_sparsity_factor=index_sparsity_factor,
                op_type=op_type,
                target_order=2,
                resolutions=[0],
                )
    else:
        raise ValueError(f"unsupported dimension: {ambient_dim}")

    logger.info("\n%s", case)

    # {{{ geometry

    dd = sym.DOFDescriptor(case.name, discr_stage=sym.QBX_SOURCE_STAGE2)
    qbx = case.get_layer_potential(actx, case.resolutions[-1], case.target_order)

    places = GeometryCollection(qbx, auto_where=(dd, dd.to_stage1()))
    density_discr = places.get_discretization(dd.geometry, dd.discr_stage)

    logger.info("nelements:     %d", density_discr.mesh.nelements)
    logger.info("ndofs:         %d", density_discr.ndofs)

    # }}}

    # {{{ symbolic

    sym_u, sym_op = case.get_operator(ambient_dim)

    from pytential.symbolic.execution import _prepare_expr
    sym_prep_op = _prepare_expr(places, sym_op)

    # }}}

    # {{{ matrix

    index_set = case.get_block_indices(actx, density_discr)
    kwargs = dict(
            dep_expr=sym_u,
            other_dep_exprs=[],
            dep_source=places.get_geometry(dd.geometry),
            dep_discr=density_discr,
            places=places,
            context=case.knl_concrete_kwargs
            )

    if block_builder_type == "qbx":
        from pytential.symbolic.matrix import MatrixBuilder
        from pytential.symbolic.matrix import \
                NearFieldBlockBuilder as BlockMatrixBuilder
    elif block_builder_type == "p2p":
        from pytential.symbolic.matrix import P2PMatrixBuilder as MatrixBuilder
        from pytential.symbolic.matrix import \
                FarFieldBlockBuilder as BlockMatrixBuilder
        kwargs["exclude_self"] = True
    else:
        raise ValueError(f"unknown block builder type: '{block_builder_type}'")

    mat = MatrixBuilder(actx, **kwargs)(sym_prep_op)
    blk = BlockMatrixBuilder(actx, index_set=index_set, **kwargs)(sym_prep_op)

    # }}}

    # {{{ check

    if visualize and ambient_dim == 2:
        try:
            import matplotlib.pyplot as pt
        except ImportError:
            visualize = False

    index_set = index_set.get(actx.queue)
    if visualize and ambient_dim == 2:
        blk_full = np.zeros_like(mat)
        mat_full = np.zeros_like(mat)

        for i in range(index_set.nblocks):
            itgt, isrc = index_set.block_indices(i)

            blk_full[np.ix_(itgt, isrc)] = index_set.block_take(blk, i)
            mat_full[np.ix_(itgt, isrc)] = index_set.take(mat, i)

        _, (ax1, ax2) = pt.subplots(1, 2,
                figsize=(10, 8), dpi=300, constrained_layout=True)
        ax1.imshow(blk_full)
        ax1.set_title(type(BlockMatrixBuilder).__name__)
        ax2.imshow(mat_full)
        ax2.set_title(type(MatrixBuilder).__name__)

        filename = f"matrix_block_{block_builder_type}_{ambient_dim}d"
        pt.savefig(filename)

    assert extra.max_block_error(mat, blk, index_set) < 1.0e-14

    # }}}


@pytest.mark.parametrize(('source_discr_stage', 'target_discr_stage'), [
    (sym.QBX_SOURCE_STAGE1, sym.QBX_SOURCE_STAGE1),
    (sym.QBX_SOURCE_STAGE2, sym.QBX_SOURCE_STAGE2),
    # (sym.QBX_SOURCE_STAGE2, sym.QBX_SOURCE_STAGE1),
    ])
def test_build_matrix_fixed_stage(ctx_factory,
        source_discr_stage, target_discr_stage, visualize=False):
    """Checks that the block builders match for difference stages."""

    ctx = ctx_factory()
    queue = cl.CommandQueue(ctx)
    actx = PyOpenCLArrayContext(queue)

    # prevent cache explosion
    from sympy.core.cache import clear_cache
    clear_cache()

    case = extra.CurveTestCase(
            name="starfish",
            curve_fn=NArmedStarfish(5, 0.25),

            target_order=4,
            resolutions=[32],

            index_sparsity_factor=0.6,
            op_type="scalar",
            tree_kind=None,
            )

    logger.info("\n%s", case)

    # {{{ geometry

    dd = sym.DOFDescriptor(case.name)
    qbx = case.get_layer_potential(actx, case.resolutions[-1], case.target_order)

    places = GeometryCollection({case.name: qbx},
            auto_where=(
                dd.copy(discr_stage=source_discr_stage),
                dd.copy(discr_stage=target_discr_stage)))

    dd = places.auto_source
    density_discr = places.get_discretization(dd.geometry, dd.discr_stage)

    # }}}

    # {{{ symbolic

    if source_discr_stage is target_discr_stage:
        qbx_forced_limit = -1
    else:
        qbx_forced_limit = None

    sym_u, sym_op = case.get_operator(places.ambient_dim, qbx_forced_limit)

    from pytential.symbolic.execution import _prepare_expr
    sym_prep_op = _prepare_expr(places, sym_op)

    # }}}

    # {{{ check

    source_discr = places.get_discretization(case.name, source_discr_stage)
    target_discr = places.get_discretization(case.name, target_discr_stage)

    logger.info("nelements:     %d", density_discr.mesh.nelements)
    logger.info("ndofs:         %d", source_discr.ndofs)
    logger.info("ndofs:         %d", target_discr.ndofs)

    icols = case.get_block_indices(actx, source_discr, matrix_indices=False)
    irows = case.get_block_indices(actx, target_discr, matrix_indices=False)
    index_set = MatrixBlockIndexRanges(actx.context, icols, irows)

    kwargs = dict(
            dep_expr=sym_u,
            other_dep_exprs=[],
            dep_source=places.get_geometry(case.name),
            dep_discr=density_discr,
            places=places,
            context=case.knl_concrete_kwargs,
            )

    # qbx
    from pytential.symbolic import matrix
    mat = matrix.MatrixBuilder(
            actx, **kwargs)(sym_prep_op)
    blk = matrix.NearFieldBlockBuilder(
            actx, index_set=index_set, **kwargs)(sym_prep_op)

    assert mat.shape == (target_discr.ndofs, source_discr.ndofs)
    assert extra.max_block_error(mat, blk, index_set.get(queue)) < 1.0e-14

    # p2p
    mat = matrix.P2PMatrixBuilder(
            actx, exclude_self=True, **kwargs)(sym_prep_op)
    blk = matrix.FarFieldBlockBuilder(
            actx, index_set=index_set, exclude_self=True, **kwargs)(sym_prep_op)

    assert mat.shape == (target_discr.ndofs, source_discr.ndofs)
    assert extra.max_block_error(mat, blk, index_set.get(queue)) < 1.0e-14

    # }}}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        exec(sys.argv[1])
    else:
        from pytest import main
        main([__file__])

# vim: fdm=marker
