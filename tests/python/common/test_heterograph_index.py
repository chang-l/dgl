import itertools
import multiprocessing as mp
import unittest
from collections import Counter

import backend as F

import dgl
import dgl.function as fn
import networkx as nx
import numpy as np
import pytest
import scipy.sparse as ssp
import test_utils
from dgl import DGLError
from scipy.sparse import rand
from test_utils import get_cases, parametrize_idtype
from utils import assert_is_identical_hetero


def create_test_heterograph(idtype):
    # test heterograph from the docstring, plus a user -- wishes -- game relation
    # 3 users, 2 games, 2 developers
    # metagraph:
    #    ('user', 'follows', 'user'),
    #    ('user', 'plays', 'game'),
    #    ('user', 'wishes', 'game'),
    #    ('developer', 'develops', 'game')])

    g = dgl.heterograph(
        {
            ("user", "follows", "user"): ([0, 1], [1, 2]),
            ("user", "plays", "game"): ([0, 1, 2, 1], [0, 0, 1, 1]),
            ("user", "wishes", "game"): ([0, 2], [1, 0]),
            ("developer", "develops", "game"): ([0, 1], [0, 1]),
        },
        idtype=idtype,
        device=F.ctx(),
    )
    assert g.idtype == idtype
    assert g.device == F.ctx()
    return g


@unittest.skipIf(
    F._default_context_str == "cpu", reason="Need gpu for this test"
)
@unittest.skipIf(
    dgl.backend.backend_name != "pytorch",
    reason="Pinning graph outplace only supported for PyTorch",
)
@parametrize_idtype
def test_pin_memory(idtype):
    # TODO: rewrite this test case to accept different graphs so we
    #  can test reverse graph and batched graph
    g = create_test_heterograph(idtype)
    g.nodes["user"].data["h"] = F.ones((3, 5))
    g.nodes["game"].data["i"] = F.ones((2, 5))
    g.edges["plays"].data["e"] = F.ones((4, 4))
    g = g.to(F.cpu())
    assert not g.is_pinned()

    # pin a CPU graph
    g._graph.pin_memory()
    assert not g.is_pinned()
    g._graph = g._graph.pin_memory()
    assert g.is_pinned()
    assert g.device == F.cpu()

    # it's fine to clone with new formats, but new graphs are not pinned
    # >>> g.formats()
    # {'created': ['coo'], 'not created': ['csr', 'csc']}
    assert not g.formats("csc").is_pinned()
    assert not g.formats("csr").is_pinned()
    # 'coo' formats is already created and thus not cloned
    assert g.formats("coo").is_pinned()

    g1 = g.to(F.cuda())
    # error pinning a GPU graph
    with pytest.raises(DGLError):
        g1._graph.pin_memory()

    # test pin empty homograph
    g2 = dgl.graph(([], []))
    g2._graph = g2._graph.pin_memory()
    assert g2.is_pinned()

    # test pin heterograph with 0 edge of one relation type
    g3 = dgl.heterograph(
        {("a", "b", "c"): ([0, 1], [1, 2]), ("c", "d", "c"): ([], [])}
    ).astype(idtype)
    g3._graph = g3._graph.pin_memory()
    assert g3.is_pinned()


if __name__ == "__main__":
    pass
