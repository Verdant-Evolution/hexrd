import numpy as np
from hexrd import constants
from hexrd.utils.decorators import numba_njit_if_available

if constants.USE_NUMBA:
    from numba import prange
else:
    prange = range

@numba_njit_if_available(cache=True, nogil=True)
def cu2ro(cu):
    return cu

@numba_njit_if_available(cache=True, nogil=True)
def ro2qu(ro):
    return ro