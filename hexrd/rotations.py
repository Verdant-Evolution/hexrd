# -*- coding: utf-8 -*-
# =============================================================================
# Copyright (c) 2012, Lawrence Livermore National Security, LLC.
# Produced at the Lawrence Livermore National Laboratory.
# Written by Joel Bernier <bernier2@llnl.gov> and others.
# LLNL-CODE-529294.
# All rights reserved.
#
# This file is part of HEXRD. For details on dowloading the source,
# see the file COPYING.
#
# Please also see the file LICENSE.
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License (as published by the Free
# Software Foundation) version 2.1 dated February 1999.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the IMPLIED WARRANTY OF MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE. See the terms and conditions of the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program (see file LICENSE); if not, write to
# the Free Software Foundation, Inc., 59 Temple Place, Suite 330,
# Boston, MA 02111-1307 USA or visit <http://www.gnu.org/licenses/>.
# =============================================================================
#
# Module containing functions relevant to rotations
#
import sys

import numpy as np
from numpy import (
    arange,
    array,
    argmax,
    asarray,
    atleast_1d,
    average,
    ndarray,
    zeros,
    dot,
    pi,
    arccos,
    cos,
    sin,
    sqrt,
    sort,
    tile,
    vstack,
    hstack,
    c_,
    ix_,
    abs,
    mod,
    finfo,
    isscalar,
)
from numpy import float_ as nFloat
from numpy import int_ as nInt
from numba import njit
from scipy.optimize import leastsq
from scipy.spatial.transform import Rotation as R

from hexrd import constants as cnst
from hexrd.matrixutil import (
    columnNorm,
    unitVector,
    findDuplicateVectors,
    multMatArray,
    nullSpace,
)

# =============================================================================
# Module Data
# =============================================================================

angularUnits = 'radians'  # module-level angle units
periodDict = {'degrees': 360.0, 'radians': 2 * np.pi}
conversion_to_dict = {'degrees': cnst.r2d, 'radians': cnst.d2r}

I3 = cnst.identity_3x3  # (3, 3) identity matrix

# axes orders, all permutations
axes_orders = [
    'xyz',
    'zyx',
    'zxy',
    'yxz',
    'yzx',
    'xzy',
    'xyx',
    'xzx',
    'yxy',
    'yzy',
    'zxz',
    'zyz',
]

sq3by2 = sqrt(3.0) / 2.0
piby2 = pi / 2.0
piby3 = pi / 3.0
piby4 = pi / 4.0
piby6 = pi / 6.0

# =============================================================================
# Functions
# =============================================================================


def arccosSafe(temp):
    """
    Protect against numbers slightly larger than 1 in magnitude
    due to round-off
    """
    temp = atleast_1d(temp)
    if (abs(temp) > 1.00001).any():
        print("attempt to take arccos of %s" % temp, file=sys.stderr)
        raise RuntimeError("unrecoverable error")

    gte1 = temp >= 1.0
    lte1 = temp <= -1.0

    temp[gte1] = 1
    temp[lte1] = -1

    ang = arccos(temp)

    return ang


#
#  ==================== Quaternions
#


def _quat_to_scipy_rotation(q: np.ndarray) -> R:
    """
    Scipy has quaternions in a differnt order, this method converts them
    q must be a 2d array of shape (4, n).
    """
    return R.from_quat(np.roll(q.T, -1, axis=1))


def _scipy_rotation_to_quat(r: R) -> np.ndarray:
    quat = np.roll(np.atleast_2d(r.as_quat()), 1, axis=1).T
    # Fix quat would work, but it does too much.  Only need to check positive
    quat *= np.sign(quat[0, :])
    return quat


def fixQuat(q):
    """
    flip to positive q0 and normalize
    """
    qdims = q.ndim
    if qdims == 3:
        l, m, n = q.shape
        assert m == 4, 'your 3-d quaternion array isn\'t the right shape'
        q = q.transpose(0, 2, 1).reshape(l * n, 4).T

    qfix = unitVector(q)

    q0negative = qfix[0,] < 0
    qfix[:, q0negative] = -1 * qfix[:, q0negative]

    if qdims == 3:
        qfix = qfix.T.reshape(l, n, 4).transpose(0, 2, 1)

    return qfix


def invertQuat(q):
    """
    silly little routine for inverting a quaternion
    """
    numq = q.shape[1]

    imat = tile(vstack([-1, 1, 1, 1]), (1, numq))

    qinv = imat * q

    return fixQuat(qinv)


def misorientation(q1, q2, *args):
    """
    PARAMETERS
    ----------
    q1: array(4, 1)
        a single quaternion
    q2: array(4, n)
        array of quaternions
    *args: tuple
        there can be only one extra argument, which is 1- or 2-tuple with
        symmetries (quaternion arrays); for crystal symmetry only,
        use a 1-tuple; with both crystal and sample symmetry use a 2-tuyple

    RETURNS
    -------
    angle: array(n)
        the misorientation angle between `q1` and each quaternion in `q2`
    mis: array(4, n)
        the quaternion of the smallest misorientation angle
    """
    if not isinstance(q1, ndarray) or not isinstance(q2, ndarray):
        raise RuntimeError("quaternion args are not of type `numpy ndarray'")

    if q1.ndim != 2 or q2.ndim != 2:
        raise RuntimeError(
            "quaternion args are the wrong shape; must be 2-d (columns)"
        )

    if q1.shape[1] != 1:
        raise RuntimeError(
            "first argument should be a single quaternion, got shape %s"
            % (q1.shape,)
        )

    if len(args) == 0:
        # no symmetries; use identity
        sym = (c_[1.0, 0, 0, 0].T, c_[1.0, 0, 0, 0].T)
    else:
        sym = args[0]
        if len(sym) == 1:
            if not isinstance(sym[0], ndarray):
                raise RuntimeError("symmetry argument is not an numpy array")
            else:
                # add triclinic sample symmetry (identity)
                sym += (c_[1.0, 0, 0, 0].T,)
        elif len(sym) == 2:
            if not isinstance(sym[0], ndarray) or not isinstance(
                sym[1], ndarray
            ):
                raise RuntimeError(
                    "symmetry arguments are not an numpy arrays"
                )
        elif len(sym) > 2:
            raise RuntimeError(
                "symmetry argument has %d entries; should be 1 or 2"
                % (len(sym))
            )

    # set some lengths
    n = q2.shape[1]  # length of misorientation list
    m = sym[0].shape[1]  # crystal (right)
    p = sym[1].shape[1]  # sample  (left)

    # tile q1 inverse
    q1i = quatProductMatrix(invertQuat(q1), mult='right').squeeze()

    # convert symmetries to (4, 4) qprod matrices
    rsym = quatProductMatrix(sym[0], mult='right')
    lsym = quatProductMatrix(sym[1], mult='left')

    # Do R * Gc, store as
    # [q2[:, 0] * Gc[:, 0:m], ..., q2[:, n-1] * Gc[:, 0:m]]
    q2 = dot(rsym, q2).transpose(2, 0, 1).reshape(m * n, 4).T

    # Do Gs * (R * Gc), store as
    # [Gs[:, 0:p]*q[:,   0]*Gc[:, 0], ..., Gs[:, 0:p]*q[:,   0]*Gc[:, m-1], ...
    #  Gs[:, 0:p]*q[:, n-1]*Gc[:, 0], ..., Gs[:, 0:p]*q[:, n-1]*Gc[:, m-1]]
    q2 = dot(lsym, q2).transpose(2, 0, 1).reshape(p * m * n, 4).T

    # Calculate the class misorientations for full symmetrically equivalent
    # classes for q1 and q2.  Note the use of the fact that the application
    # of the symmetry groups is an isometry.
    eqvMis = fixQuat(dot(q1i, q2))

    # Reshape scalar comp columnwise by point in q2 (and q1, if applicable)
    sclEqvMis = eqvMis[0, :].reshape(n, p * m).T

    # Find misorientation closest to origin for each n equivalence classes
    #   - fixed quats so garaunteed that sclEqvMis is nonnegative
    qmax = sclEqvMis.max(0)

    # remap indices to use in eqvMis
    qmaxInd = (sclEqvMis == qmax).nonzero()
    qmaxInd = c_[qmaxInd[0], qmaxInd[1]]

    eqvMisColInd = sort(qmaxInd[:, 0] + qmaxInd[:, 1] * p * m)

    # store Rmin in q
    mis = eqvMis[ix_(list(range(4)), eqvMisColInd)]

    angle = 2 * arccosSafe(qmax)

    return angle, mis


def quatProduct(q1, q2):
    """
    Product of two unit quaternions.

    qp = quatProduct(q2, q1)

    q2, q1 are 4 x n, arrays whose columns are
           quaternion parameters

    qp is 4 x n, an array whose columns are the
       quaternion parameters of the product; the
       first component of qp is nonnegative

    If R(q) is the rotation corresponding to the
    quaternion parameters q, then

    R(qp) = R(q2) R(q1)
    """
    rot_1 = _quat_to_scipy_rotation(q1)
    rot_2 = _quat_to_scipy_rotation(q2)
    rot_p = rot_2 * rot_1
    return _scipy_rotation_to_quat(rot_p)


def quatProductMatrix(quats, mult='right'):
    """
    Form 4 x 4 arrays to perform the quaternion product

    USAGE
        qmats = quatProductMatrix(quats, mult='right')

    INPUTS
        1) quats is (4, n), a numpy ndarray array of n quaternions
           horizontally concatenated
        2) mult is a keyword arg, either 'left' or 'right', denoting
           the sense of the multiplication:

                       | quatProductMatrix(h, mult='right') * q
           q * h  --> <
                       | quatProductMatrix(q, mult='left') * h

    OUTPUTS
        1) qmats is (n, 4, 4), the left or right quaternion product
           operator

    NOTES
       *) This function is intended to replace a cross-product based
          routine for products of quaternions with large arrays of
          quaternions (e.g. applying symmetries to a large set of
          orientations).
    """

    if quats.shape[0] != 4:
        raise RuntimeError("input is the wrong size along the 0-axis")

    nq = quats.shape[1]
    q0 = quats[0, :].copy()
    q1 = quats[1, :].copy()
    q2 = quats[2, :].copy()
    q3 = quats[3, :].copy()
    if mult == 'right':
        qmats = array([[q0], [q1], [q2], [q3],
                       [-q1], [q0], [-q3], [q2],
                       [-q2], [q3], [q0], [-q1],
                       [-q3], [-q2], [q1], [q0]])
    elif mult == 'left':
        qmats = array([[q0], [q1], [q2], [q3],
                       [-q1], [q0], [q3], [-q2],
                       [-q2], [-q3], [q0], [q1],
                       [-q3], [q2], [-q1], [q0]])
    # some fancy reshuffling...
    qmats = qmats.T.reshape(nq, 4, 4).transpose(0, 2, 1)
    return qmats


def quatOfAngleAxis(angle, rotaxis):
    """
    make an hstacked array of quaternions from arrays of angle/axis pairs
    """
    if isinstance(angle, list):
        n = len(angle)
        angle = asarray(angle)
    elif isinstance(angle, float) or isinstance(angle, int):
        n = 1
    elif isinstance(angle, ndarray):
        n = angle.shape[0]
    else:
        raise RuntimeError(
            "angle argument is of incorrect type; "
            + "must be a list, int, float, or ndarray."
        )

    if rotaxis.shape[1] == 1:
        rotaxis = tile(rotaxis, (1, n))
    elif rotaxis.shape[1] != n:
        raise RuntimeError("rotation axes argument has incompatible shape")

    # Normalize the axes
    rotaxis = unitVector(rotaxis)
    rot = R.from_rotvec((angle * rotaxis).T)
    return _scipy_rotation_to_quat(rot)


def quatOfExpMap(expMaps):
    """
    Returns the unit quaternions associated with exponential map parameters.

    Parameters
    ----------
    expMaps : array_like
        The (3,) or (3, n) list of hstacked exponential map parameters to
        convert.

    Returns
    -------
    quats : array_like
        The (4,) or (4, n) array of unit quaternions.

    Notes
    -----
    1) be aware that the output will always have non-negative q0; recall the
       antipodal symmetry of unit quaternions

    """
    cdim = 3  # critical dimension of input
    expMaps = np.atleast_2d(expMaps)
    if len(expMaps) == 1:
        assert expMaps.shape[1] == cdim, (
            "your input quaternion must have %d elements" % cdim
        )
        expMaps = np.reshape(expMaps, (cdim, 1))
    else:
        assert len(expMaps) == cdim, (
            "your input quaternions must have shape (%d, n) for n > 1" % cdim
        )

    return _scipy_rotation_to_quat(R.from_rotvec(expMaps.T)).squeeze()


def quatOfRotMat(r_mat):
    """
    Generate quaternions from rotation matrices
    """
    return _scipy_rotation_to_quat(R.from_matrix(r_mat))


def quatAverageCluster(q_in, qsym):
    """ """
    assert q_in.ndim == 2, 'input must be 2-s hstacked quats'

    # renormalize
    q_in = unitVector(q_in)

    # check to see num of quats is > 1
    if q_in.shape[1] < 3:
        if q_in.shape[1] == 1:
            q_bar = q_in
        else:
            ma, mq = misorientation(
                q_in[:, 0].reshape(4, 1), q_in[:, 1].reshape(4, 1), (qsym,)
            )

            q_bar = quatProduct(
                q_in[:, 0].reshape(4, 1),
                quatOfExpMap(0.5 * ma * unitVector(mq[1:])).reshape(4, 1),
            )
    else:
        # first drag to origin using first quat (arb!)
        q0 = q_in[:, 0].reshape(4, 1)
        qrot = dot(quatProductMatrix(invertQuat(q0), mult='left'), q_in)

        # second, re-cast to FR
        qrot = toFundamentalRegion(qrot.squeeze(), crysSym=qsym)

        # compute arithmetic average
        q_bar = unitVector(average(qrot, axis=1).reshape(4, 1))

        # unrotate!
        q_bar = dot(quatProductMatrix(q0, mult='left'), q_bar)

        # re-map
        q_bar = toFundamentalRegion(q_bar, crysSym=qsym)
    return q_bar


def quatAverage(q_in, qsym):
    """ """
    assert q_in.ndim == 2, 'input must be 2-s hstacked quats'

    # renormalize
    q_in = unitVector(q_in)

    # check to see num of quats is > 1
    if q_in.shape[1] < 3:
        if q_in.shape[1] == 1:
            q_bar = q_in
        else:
            ma, mq = misorientation(
                q_in[:, 0].reshape(4, 1), q_in[:, 1].reshape(4, 1), (qsym,)
            )
            q_bar = quatProduct(
                q_in[:, 0].reshape(4, 1),
                quatOfExpMap(0.5 * ma * unitVector(mq[1:].reshape(3, 1))),
            )
    else:
        # use first quat as initial guess
        phi = 2.0 * arccos(q_in[0, 0])
        if phi <= finfo(float).eps:
            x0 = zeros(3)
        else:
            n = unitVector(q_in[1:, 0].reshape(3, 1))
            x0 = phi * n.flatten()
        results = leastsq(quatAverage_obj, x0, args=(q_in, qsym))
        phi = sqrt(sum(results[0] * results[0]))
        if phi <= finfo(float).eps:
            q_bar = c_[1.0, 0.0, 0.0, 0.0].T
        else:
            n = results[0] / phi
            q_bar = hstack([cos(0.5 * phi), sin(0.5 * phi) * n]).reshape(4, 1)
    return q_bar


def quatAverage_obj(xi_in, quats, qsym):
    phi = sqrt(sum(xi_in.flatten() * xi_in.flatten()))
    if phi <= finfo(float).eps:
        q0 = c_[1.0, 0.0, 0.0, 0.0].T
    else:
        n = xi_in.flatten() / phi
        q0 = hstack([cos(0.5 * phi), sin(0.5 * phi) * n])
    resd = misorientation(q0.reshape(4, 1), quats, (qsym,))[0]
    return resd


def expMapOfQuat(quats):
    """
    Return the exponential map parameters for an array of unit quaternions

    Parameters
    ----------
    quats : array_like
        The (4, ) or (4, n) array of hstacked unit quaternions.  The convention
        is [q0, q] where q0 is the scalar part and q is the vector part.

    Returns
    -------
    expmaps : array_like
        The (3, ) or (3, n) array of exponential map parameters associated
        with the input quaternions.

    """
    cdim = 4  # critical dimension of input
    quats = np.atleast_2d(quats)
    if len(quats) == 1:
        assert quats.shape[1] == cdim, (
            "your input quaternion must have %d elements" % cdim
        )
        quats = np.reshape(quats, (cdim, 1))
    else:
        assert len(quats) == cdim, (
            "your input quaternions must have shape (%d, n) for n > 1" % cdim
        )

    return _quat_to_scipy_rotation(quats).as_rotvec().T.squeeze()


def rotMatOfExpMap(expMap):
    """
    Make a rotation matrix from an expmap
    """
    if expMap.ndim == 1:
        expMap = expMap.reshape(3, 1)

    return R.from_rotvec(expMap.T).as_matrix().squeeze()


@njit(cache=True, nogil=True)
def _rotmatofquat(quat):
    n = quat.shape[1]
    # FIXME: maybe preallocate for speed?
    # R = zeros(n*3*3, dtype='float64')

    a = np.ascontiguousarray(quat[0, :]).reshape(n, 1)
    b = np.ascontiguousarray(quat[1, :]).reshape(n, 1)
    c = np.ascontiguousarray(quat[2, :]).reshape(n, 1)
    d = np.ascontiguousarray(quat[3, :]).reshape(n, 1)

    R = np.hstack(
        (
            a**2 + b**2 - c**2 - d**2,
            2 * b * c - 2 * a * d,
            2 * a * c + 2 * b * d,
            2 * a * d + 2 * b * c,
            a**2 - b**2 + c**2 - d**2,
            2 * c * d - 2 * a * b,
            2 * b * d - 2 * a * c,
            2 * a * b + 2 * c * d,
            a**2 - b**2 - c**2 + d**2,
        )
    )

    return R.reshape(n, 3, 3)


def rotMatOfQuat(quat):
    """
    Convert quaternions to rotation matrices.

    Take an array of n quats (numpy ndarray, 4 x n) and generate an
    array of rotation matrices (n x 3 x 3)

    Parameters
    ----------
    quat : TYPE
        DESCRIPTION.

    Raises
    ------
    RuntimeError
        DESCRIPTION.

    Returns
    -------
    rmat : TYPE
        DESCRIPTION.

    Notes
    -----
    Uses the truncated series expansion for the exponential map;
    didvide-by-zero is checked using the global 'cnst.epsf'
    """
    if quat.ndim == 1:
        if len(quat) != 4:
            raise RuntimeError("input is the wrong shape")
        else:
            quat = quat.reshape(4, 1)
    else:
        if quat.shape[0] != 4:
            raise RuntimeError("input is the wrong shape")

    rmat = _rotmatofquat(quat)

    return np.squeeze(rmat)


def angleAxisOfRotMat(rot_mat):
    """
    Extracts angle and axis invariants from rotation matrices.

    Parameters
    ----------
    R : numpy.ndarray
        The (3, 3) or (n, 3, 3) array of rotation matrices.
        Note that these are assumed to be proper orthogonal.

    Raises
    ------
    RuntimeError
        If `R` is not an shape is not (3, 3) or (n, 3, 3).

    Returns
    -------
    phi : numpy.ndarray
        The (n, ) array of rotation angles for the n input
        rotation matrices.
    n : numpy.ndarray
        The (3, n) array of unit rotation axes for the n
        input rotation matrices.

    """
    if not isinstance(rot_mat, ndarray):
        raise RuntimeError('Input must be a 2 or 3-d ndarray')
    else:
        rdim = rot_mat.ndim
        if rdim == 2:
            rot_mat = tile(rot_mat, (1, 1, 1))
        elif rdim == 3:
            pass
        else:
            raise RuntimeError(
                "rot_mat array must be (3, 3) or (n, 3, 3); input has dimension %d"
                % (rdim)
            )

    rot_vec = R.from_matrix(rot_mat).as_rotvec()
    angs = np.linalg.norm(rot_vec, axis=1)
    axes = unitVector(rot_vec.T)
    return angs, axes


def _check_axes_order(x):
    if not isinstance(x, str):
        raise RuntimeError("argument must be str")
    axo = x.lower()
    if axo not in axes_orders:
        raise RuntimeError("order '%s' is not a valid choice" % x)
    return axo


def _check_is_rmat(x):
    x = np.asarray(x)
    if x.shape != (3, 3):
        raise RuntimeError("shape of input must be (3, 3)")
    chk1 = np.linalg.det(x)
    chk2 = np.sum(abs(np.eye(3) - np.dot(x, x.T)))
    if 1.0 - abs(chk1) < cnst.sqrt_epsf and chk2 < cnst.sqrt_epsf:
        return x
    else:
        raise RuntimeError("input is not an orthogonal matrix")


def make_rmat_euler(tilt_angles, axes_order, extrinsic=True):
    """
    Generate rotation matrix from Euler angles.

    Parameters
    ----------
    tilt_angles : array_like
        The (3, ) list of Euler angles in RADIANS.
    axes_order : str
        The axes order specification (case-insensitive).  This must be one
        of the following: 'xyz', 'zyx'
                          'zxy', 'yxz'
                          'yzx', 'xzy'
                          'xyx', 'xzx'
                          'yxy', 'yzy'
                          'zxz', 'zyz'
    extrinsic : bool, optional
        Flag denoting the convention.  If True, the convention is
        extrinsic (passive); if False, the convention is
        instrinsic (active). The default is True.

    Returns
    -------
    numpy.ndarray
        The (3, 3) rotation matrix corresponding to the input specification.

    TODO: add kwarg for unit selection for `tilt_angles`
    TODO: input checks
    """
    axo = _check_axes_order(axes_order)
    if not extrinsic:
        axo = axo.upper()

    return R.from_euler(axo, tilt_angles).as_matrix()


def angles_from_rmat_xyz(rmat):
    """
    Calculate passive x-y-z Euler angles from a rotation matrix.

    Parameters
    ----------
    rmat : TYPE
        DESCRIPTION.

    Returns
    -------
    rx : TYPE
        DESCRIPTION.
    ry : TYPE
        DESCRIPTION.
    rz : TYPE
        DESCRIPTION.

    """
    rmat = _check_is_rmat(rmat)

    return R.from_matrix(rmat).as_euler('xyz')


def angles_from_rmat_zxz(rmat):
    """
    Calculate active z-x-z Euler angles from a rotation matrix.

    Parameters
    ----------
    rmat : TYPE
        DESCRIPTION.

    Returns
    -------
    alpha : TYPE
        DESCRIPTION.
    beta : TYPE
        DESCRIPTION.
    gamma : TYPE
        DESCRIPTION.

    """
    rmat = _check_is_rmat(rmat)

    return R.from_matrix(rmat).as_euler('ZXZ')


class RotMatEuler(object):
    def __init__(self, angles, axes_order, extrinsic=True, units=angularUnits):
        """
        Abstraction of a rotation matrix defined by Euler angles.

        Parameters
        ----------
        angles : array_like
            The (3, ) list of Euler angles in RADIANS.
        axes_order : str
            The axes order specification (case-insensitive).  This must be one
            of the following:

                'xyz', 'zyx'
                'zxy', 'yxz'
                'yzx', 'xzy'
                'xyx', 'xzx'
                'yxy', 'yzy'
                'zxz', 'zyz'

        extrinsic : bool, optional
            Flag denoting the convention.  If True, the convention is
            extrinsic (passive); if False, the convention is
            instrinsic (active). The default is True.

        Returns
        -------
        None.

        TODO: add check that angle input is array-like, len() = 3?
        TODO: add check on extrinsic as bool
        """
        self._axes = np.eye(3)
        self._axes_dict = dict(x=0, y=1, z=2)

        # these will be properties
        self._angles = angles
        self._axes_order = _check_axes_order(axes_order)
        self._extrinsic = extrinsic
        if units.lower() not in periodDict.keys():
            raise RuntimeError("angular units '%s' not understood" % units)
        self._units = units

    @property
    def angles(self):
        return self._angles

    @angles.setter
    def angles(self, x):
        x = np.atleast_1d(x).flatten()
        if len(x) == 3:
            self._angles = x
        else:
            raise RuntimeError("input must be array-like with __len__ = 3")

    @property
    def axes_order(self):
        return self._axes_order

    @axes_order.setter
    def axes_order(self, x):
        axo = _check_axes_order(x)
        self._axes_order = axo

    @property
    def extrinsic(self):
        return self._extrinsic

    @extrinsic.setter
    def extrinsic(self, x):
        if isinstance(x, bool):
            self._extrinsic = x
        else:
            raise RuntimeError("input must be a bool")

    @property
    def units(self):
        return self._units

    @units.setter
    def units(self, x):
        if isinstance(x, str) and x in periodDict.keys():
            if self._units != x:
                # !!! we are changing units; update self.angles
                self.angles = conversion_to_dict[x] * np.asarray(self.angles)
            self._units = x
        else:
            raise RuntimeError("input must be 'degrees' or 'radians'")

    @property
    def rmat(self):
        """
        Return the rotation matrix.

        As calculated from angles, axes_order, and convention.

        Returns
        -------
        numpy.ndarray
            The (3, 3) proper orthogonal matrix according to the specification.

        """
        angs_in = self.angles
        if self.units == 'degrees':
            angs_in = conversion_to_dict['radians'] * angs_in
        self._rmat = make_rmat_euler(angs_in, self.axes_order, self.extrinsic)
        return self._rmat

    @rmat.setter
    def rmat(self, x):
        """
        Update class via input rotation matrix.

        Parameters
        ----------
        x : array_like
            A (3, 3) array to be interpreted as a rotation matrix.

        Returns
        -------
        None.WW
        """
        rmat = _check_is_rmat(x)
        self._rmat = rmat

        axo = self.axes_order
        if not self.extrinsic:
            axo = axo.upper()
        self._angles = R.from_matrix(rmat).as_euler(
            axo, self.units == 'degrees'
        )

    @property
    def exponential_map(self):
        """
        The matrix invariants of self.rmat as exponential map parameters

        Returns
        -------
        np.ndarray
            The (3, ) array representing the exponential map parameters of
            the encoded rotation (self.rmat).

        """
        phi, n = angleAxisOfRotMat(self.rmat)
        return phi * n.flatten()

    @exponential_map.setter
    def exponential_map(self, x):
        """
        Updates encoded rotation via exponential map parameters

        Parameters
        ----------
        x : array_like
            The (3, ) vector representing exponential map parameters of a
            rotation.

        Returns
        -------
        None.

        Notes
        -----
        Updates the encoded rotation from expoential map parameters via
        self.rmat property
        """
        x = np.atleast_1d(x).flatten()
        assert len(x) == 3, "input must have exactly 3 elements"
        self.rmat = rotMatOfExpMap(x.reshape(3, 1))  # use local func


#
#  ==================== Fiber
#


def distanceToFiber(c, s, q, qsym, **kwargs):
    """
    Calculate symmetrically reduced distance to orientation fiber.

    Parameters
    ----------
    c : TYPE
        DESCRIPTION.
    s : TYPE
        DESCRIPTION.
    q : TYPE
        DESCRIPTION.
    qsym : TYPE
        DESCRIPTION.
    **kwargs : TYPE
        DESCRIPTION.

    Raises
    ------
    RuntimeError
        DESCRIPTION.

    Returns
    -------
    d : TYPE
        DESCRIPTION.

    """
    csymFlag = False
    B = I3

    arglen = len(kwargs)

    if len(c) != 3 or len(s) != 3:
        raise RuntimeError('c and/or s are not 3-vectors')

    # argument handling
    if arglen > 0:
        argkeys = list(kwargs.keys())
        for i in range(arglen):
            if argkeys[i] == 'centrosymmetry':
                csymFlag = kwargs[argkeys[i]]
            elif argkeys[i] == 'bmatrix':
                B = kwargs[argkeys[i]]
            else:
                raise RuntimeError(
                    "keyword arg \'%s\' is not recognized" % (argkeys[i])
                )

    c = unitVector(dot(B, asarray(c)))
    s = unitVector(asarray(s).reshape(3, 1))

    nq = q.shape[1]  # number of quaternions
    rmats = rotMatOfQuat(q)  # (nq, 3, 3)

    csym = applySym(c, qsym, csymFlag)  # (3, m)
    m = csym.shape[1]  # multiplicity

    if nq == 1:
        rc = dot(rmats, csym)  # apply q's to c's

        sdotrc = dot(s.T, rc).max()
    else:
        rc = multMatArray(rmats, tile(csym, (nq, 1, 1)))  # apply q's to c's

        sdotrc = (
            dot(s.T, rc.swapaxes(1, 2).reshape(nq * m, 3).T)
            .reshape(nq, m)
            .max(1)
        )

    d = arccosSafe(array(sdotrc))

    return d


def discreteFiber(c, s, B=I3, ndiv=120, invert=False, csym=None, ssym=None):
    """
    Generate symmetrically reduced discrete orientation fiber.

    Parameters
    ----------
    c : TYPE
        DESCRIPTION.
    s : TYPE
        DESCRIPTION.
    B : TYPE, optional
        DESCRIPTION. The default is I3.
    ndiv : TYPE, optional
        DESCRIPTION. The default is 120.
    invert : TYPE, optional
        DESCRIPTION. The default is False.
    csym : TYPE, optional
        DESCRIPTION. The default is None.
    ssym : TYPE, optional
        DESCRIPTION. The default is None.

    Raises
    ------
    RuntimeError
        DESCRIPTION.

    Returns
    -------
    retval : TYPE
        DESCRIPTION.

    """

    ztol = cnst.sqrt_epsf

    # arg handling for c
    if hasattr(c, '__len__'):
        if hasattr(c, 'shape'):
            assert (
                c.shape[0] == 3
            ), 'scattering vector must be 3-d; yours is %d-d' % (c.shape[0])
            if len(c.shape) == 1:
                c = c.reshape(3, 1)
            elif len(c.shape) > 2:
                raise RuntimeError(
                    'incorrect arg shape; must be 1-d or 2-d, yours is %d-d'
                    % (len(c.shape))
                )
        else:
            # convert list input to array and transpose
            if len(c) == 3 and isscalar(c[0]):
                c = asarray(c).reshape(3, 1)
            else:
                c = asarray(c).T
    else:
        raise RuntimeError('input must be array-like')

    # arg handling for s
    if hasattr(s, '__len__'):
        if hasattr(s, 'shape'):
            assert (
                s.shape[0] == 3
            ), 'scattering vector must be 3-d; yours is %d-d' % (s.shape[0])
            if len(s.shape) == 1:
                s = s.reshape(3, 1)
            elif len(s.shape) > 2:
                raise RuntimeError(
                    'incorrect arg shape; must be 1-d or 2-d, yours is %d-d'
                    % (len(s.shape))
                )
        else:
            # convert list input to array and transpose
            if len(s) == 3 and isscalar(s[0]):
                s = asarray(s).reshape(3, 1)
            else:
                s = asarray(s).T
    else:
        raise RuntimeError('input must be array-like')

    nptc = c.shape[1]
    npts = s.shape[1]

    c = unitVector(dot(B, c))  # turn c hkls into unit vector in crys frame
    s = unitVector(s)  # convert s to unit vector in samp frame

    retval = []
    for i_c in range(nptc):
        dupl_c = tile(c[:, i_c], (npts, 1)).T

        ax = s + dupl_c
        anrm = columnNorm(ax).squeeze()  # should be 1-d

        okay = anrm > ztol
        nokay = okay.sum()
        if nokay == npts:
            ax = ax / tile(anrm, (3, 1))
        else:
            nspace = nullSpace(c[:, i_c].reshape(3, 1))
            hperp = nspace[:, 0].reshape(3, 1)
            if nokay == 0:
                ax = tile(hperp, (1, npts))
            else:
                ax[:, okay] = ax[:, okay] / tile(anrm[okay], (3, 1))
                ax[:, not okay] = tile(hperp, (1, npts - nokay))

        q0 = vstack([zeros(npts), ax])

        # find rotations
        # note: the following line fixes bug with use of arange
        # with float increments
        phi = arange(0, ndiv) * (2 * pi / float(ndiv))
        qh = quatOfAngleAxis(phi, tile(c[:, i_c], (ndiv, 1)).T)

        # the fibers, arraged as (npts, 4, ndiv)
        qfib = dot(quatProductMatrix(qh, mult='right'), q0).transpose(2, 1, 0)
        if csym is not None:
            retval.append(
                toFundamentalRegion(qfib.squeeze(), crysSym=csym, sampSym=ssym)
            )
        else:
            retval.append(fixQuat(qfib).squeeze())
    return retval


#
#  ==================== Utility Functions
#


def mapAngle(ang, *args, **kwargs):
    """
    Utility routine to map an angle into a specified period
    """
    period = 2.0 * pi  # radians
    units = angularUnits  # usually

    kwargKeys = list(kwargs.keys())
    for iArg in range(len(kwargKeys)):
        if kwargKeys[iArg] == 'units':
            units = kwargs[kwargKeys[iArg]]
        else:
            raise RuntimeError(
                "Unknown keyword argument: " + str(kwargKeys[iArg])
            )

    if units.lower() == 'degrees':
        period = 360.0
    elif units.lower() != 'radians':
        raise RuntimeError(
            "unknown angular units: " + str(kwargs[kwargKeys[iArg]])
        )

    ang = atleast_1d(nFloat(ang))

    # if we have a specified angular range, use that
    if len(args) > 0:
        angRange = atleast_1d(nFloat(args[0]))

        # divide of multiples of period
        ang = ang - nInt(np.nan_to_num(ang) / period) * period

        lb = angRange.min()
        ub = angRange.max()

        if abs(ub - lb) != period:
            raise RuntimeError('range is incomplete!')

        lbi = ang < lb
        while lbi.sum() > 0:
            ang[lbi] = ang[lbi] + period
            lbi = ang < lb
        ubi = ang > ub
        while ubi.sum() > 0:
            ang[ubi] = ang[ubi] - period
            ubi = ang > ub
        retval = ang
    else:
        retval = mod(ang + 0.5 * period, period) - 0.5 * period
    return retval


def angularDifference_orig(angList0, angList1, units=angularUnits):
    """
    Do the proper (acute) angular difference in the context of a branch cut.

    *) Default angular range in the code is [-pi, pi]
    *) ... maybe more efficient not to vectorize?
    """
    if units == 'radians':
        period = 2 * pi
    elif units == 'degrees':
        period = 360.0
    else:
        raise RuntimeError(
            "'%s' is an unrecognized option for angular units!" % (units)
        )

    # take difference as arrays
    diffAngles = asarray(angList0) - asarray(angList1)

    return abs(mod(diffAngles + 0.5 * period, period) - 0.5 * period)


def angularDifference_opt(angList0, angList1, units=angularUnits):
    """
    Do the proper (acute) angular difference in the context of a branch cut.

    *) Default angular range in the code is [-pi, pi]
    """
    period = periodDict[units]
    d = abs(angList1 - angList0)
    return np.minimum(d, period - d)


angularDifference = angularDifference_opt


def applySym(vec, qsym, csFlag=False, cullPM=False, tol=cnst.sqrt_epsf):
    """
    Apply symmetry group to a single 3-vector (columnar) argument.

    csFlag : centrosymmetry flag
    cullPM : cull +/- flag
    """
    nsym = qsym.shape[1]
    Rsym = rotMatOfQuat(qsym)
    if nsym == 1:
        Rsym = array(
            [
                Rsym,
            ]
        )
    allhkl = (
        multMatArray(Rsym, tile(vec, (nsym, 1, 1)))
        .swapaxes(1, 2)
        .reshape(nsym, 3)
        .T
    )

    if csFlag:
        allhkl = hstack([allhkl, -1 * allhkl])
    eqv, uid = findDuplicateVectors(allhkl, tol=tol, equivPM=cullPM)

    return allhkl[ix_(list(range(3)), uid)]


# =============================================================================
# Symmetry functions
# =============================================================================


def toFundamentalRegion(q, crysSym='Oh', sampSym=None):
    """
    Map quaternions to fundamental region.

    Parameters
    ----------
    q : TYPE
        DESCRIPTION.
    crysSym : TYPE, optional
        DESCRIPTION. The default is 'Oh'.
    sampSym : TYPE, optional
        DESCRIPTION. The default is None.

    Raises
    ------
    NotImplementedError
        DESCRIPTION.

    Returns
    -------
    qr : TYPE
        DESCRIPTION.
    """
    qdims = q.ndim
    if qdims == 3:
        l3, m3, n3 = q.shape
        assert m3 == 4, 'your 3-d quaternion array isn\'t the right shape'
        q = q.transpose(0, 2, 1).reshape(l3 * n3, 4).T
    if isinstance(crysSym, str):
        qsym_c = quatProductMatrix(
            quatOfLaueGroup(crysSym), 'right'
        )  # crystal symmetry operator
    else:
        qsym_c = quatProductMatrix(crysSym, 'right')

    n = q.shape[1]  # total number of quats
    m = qsym_c.shape[0]  # number of symmetry operations

    #
    # MAKE EQUIVALENCE CLASS
    #
    # Do R * Gc, store as
    # [q[:, 0] * Gc[:, 0:m], ..., 2[:, n-1] * Gc[:, 0:m]]
    qeqv = dot(qsym_c, q).transpose(2, 0, 1).reshape(m * n, 4).T

    if sampSym is None:
        # need to fix quats to sort
        qeqv = fixQuat(qeqv)

        # Reshape scalar comp columnwise by point in qeqv
        q0 = qeqv[0, :].reshape(n, m).T

        # Find q0 closest to origin for each n equivalence classes
        q0maxColInd = argmax(q0, 0) + [x * m for x in range(n)]

        # store representatives in qr
        qr = qeqv[:, q0maxColInd]
    else:
        if isinstance(sampSym, str):
            qsym_s = quatProductMatrix(
                quatOfLaueGroup(sampSym), 'left'
            )  # sample symmetry operator
        else:
            qsym_s = quatProductMatrix(sampSym, 'left')

        p = qsym_s.shape[0]  # number of sample symmetry operations

        # Do Gs * (R * Gc), store as
        # [Gs[:, 0:p]*q[:,   0]*Gc[:, 0], ..., Gs[:, 0:p]*q[:,   0]*Gc[:, m-1],
        #  ...,
        #  Gs[:, 0:p]*q[:, n-1]*Gc[:, 0], ..., Gs[:, 0:p]*q[:, n-1]*Gc[:, m-1]]
        qeqv = fixQuat(
            dot(qsym_s, qeqv).transpose(2, 0, 1).reshape(p * m * n, 4).T
        )

        raise NotImplementedError

    # debug
    assert qr.shape[1] == n, 'oops, something wrong here with your reshaping'

    if qdims == 3:
        qr = qr.T.reshape(l3, n3, 4).transpose(0, 2, 1)

    return qr


def ltypeOfLaueGroup(tag):
    """
    Yield lattice type of input tag.

    Parameters
    ----------
    tag : TYPE
        DESCRIPTION.

    Raises
    ------
    RuntimeError
        DESCRIPTION.

    Returns
    -------
    ltype : TYPE
        DESCRIPTION.

    """
    if not isinstance(tag, str):
        raise RuntimeError("entered flag is not a string!")

    if tag.lower() == 'ci' or tag.lower() == 's2':
        ltype = 'triclinic'
    elif tag.lower() == 'c2h':
        ltype = 'monoclinic'
    elif tag.lower() == 'd2h' or tag.lower() == 'vh':
        ltype = 'orthorhombic'
    elif tag.lower() == 'c4h' or tag.lower() == 'd4h':
        ltype = 'tetragonal'
    elif tag.lower() == 'c3i' or tag.lower() == 's6' or tag.lower() == 'd3d':
        ltype = 'trigonal'
    elif tag.lower() == 'c6h' or tag.lower() == 'd6h':
        ltype = 'hexagonal'
    elif tag.lower() == 'th' or tag.lower() == 'oh':
        ltype = 'cubic'
    else:
        raise RuntimeError(
            "unrecognized symmetry group.  "
            + "See ''help(quatOfLaueGroup)'' for a list of valid options.  "
            + "Oh, and have a great day ;-)"
        )

    return ltype


def quatOfLaueGroup(tag):
    """
    Return quaternion representation of requested symmetry group.

    Parameters
    ----------
    tag : str
        A case-insensitive string representing the Schoenflies symbol for the
        desired Laue group.  The 14 available choices are:

              Class           Symbol      N
             -------------------------------
              Triclinic       Ci (S2)     1
              Monoclinic      C2h         2
              Orthorhombic    D2h (Vh)    4
              Tetragonal      C4h         4
                              D4h         8
              Trigonal        C3i (S6)    3
                              D3d         6
              Hexagonal       C6h         6
                              D6h         12
              Cubic           Th          12
                              Oh          24

    Raises
    ------
    RuntimeError
        For invalid symmetry group tag.

    Returns
    -------
    qsym : (4, N) ndarray
        the quaterions associated with each element of the chosen symmetry
        group having n elements (dep. on group -- see INPUTS list above).

    Notes
    -----
    The conventions used for assigning a RHON basis, {x1, x2, x3}, to each
    point group are consistent with those published in Appendix B of [1]_.

    References
    ----------
    [1] Nye, J. F., ``Physical Properties of Crystals: Their
    Representation by Tensors and Matrices'', Oxford University Press,
    1985. ISBN 0198511655
    """
    if not isinstance(tag, str):
        raise RuntimeError("entered flag is not a string!")

    if tag.lower() == 'ci' or tag.lower() == 's2':
        # TRICLINIC
        angleAxis = vstack([0.0, 1.0, 0.0, 0.0])  # identity
    elif tag.lower() == 'c2h':
        # MONOCLINIC
        angleAxis = c_[
            [0.0, 1, 0, 0],  # identity
            [pi, 0, 1, 0],  # twofold about 010 (x2)
        ]
    elif tag.lower() == 'd2h' or tag.lower() == 'vh':
        # ORTHORHOMBIC
        angleAxis = c_[
            [0.0, 1, 0, 0],  # identity
            [pi, 1, 0, 0],  # twofold about 100
            [pi, 0, 1, 0],  # twofold about 010
            [pi, 0, 0, 1],  # twofold about 001
        ]
    elif tag.lower() == 'c4h':
        # TETRAGONAL (LOW)
        angleAxis = c_[
            [0.0, 1, 0, 0],  # identity
            [piby2, 0, 0, 1],  # fourfold about 001 (x3)
            [pi, 0, 0, 1],  #
            [piby2 * 3, 0, 0, 1],  #
        ]
    elif tag.lower() == 'd4h':
        # TETRAGONAL (HIGH)
        angleAxis = c_[
            [0.0, 1, 0, 0],  # identity
            [piby2, 0, 0, 1],  # fourfold about 0  0  1 (x3)
            [pi, 0, 0, 1],  #
            [piby2 * 3, 0, 0, 1],  #
            [pi, 1, 0, 0],  # twofold about  1  0  0 (x1)
            [pi, 0, 1, 0],  # twofold about  0  1  0 (x2)
            [pi, 1, 1, 0],  # twofold about  1  1  0
            [pi, -1, 1, 0],  # twofold about -1  1  0
        ]
    elif tag.lower() == 'c3i' or tag.lower() == 's6':
        # TRIGONAL (LOW)
        angleAxis = c_[
            [0.0, 1, 0, 0],  # identity
            [piby3 * 2, 0, 0, 1],  # threefold about 0001 (x3,c)
            [piby3 * 4, 0, 0, 1],  #
        ]
    elif tag.lower() == 'd3d':
        # TRIGONAL (HIGH)
        angleAxis = c_[
            [0.0, 1, 0, 0],  # identity
            [piby3 * 2, 0, 0, 1],  # threefold about 0001 (x3,c)
            [piby3 * 4, 0, 0, 1],  #
            [pi, 1, 0, 0],  # twofold about  2 -1 -1  0 (x1,a1)
            [pi, -0.5, sq3by2, 0],  # twofold about -1  2 -1  0 (a2)
            [pi, -0.5, -sq3by2, 0],  # twofold about -1 -1  2  0 (a3)
        ]
    elif tag.lower() == 'c6h':
        # HEXAGONAL (LOW)
        angleAxis = c_[
            [0.0, 1, 0, 0],  # identity
            [piby3, 0, 0, 1],  # sixfold about 0001 (x3,c)
            [piby3 * 2, 0, 0, 1],  #
            [pi, 0, 0, 1],  #
            [piby3 * 4, 0, 0, 1],  #
            [piby3 * 5, 0, 0, 1],  #
        ]
    elif tag.lower() == 'd6h':
        # HEXAGONAL (HIGH)
        angleAxis = c_[
            [0.0, 1, 0, 0],  # identity
            [piby3, 0, 0, 1],  # sixfold about  0  0  1 (x3,c)
            [piby3 * 2, 0, 0, 1],  #
            [pi, 0, 0, 1],  #
            [piby3 * 4, 0, 0, 1],  #
            [piby3 * 5, 0, 0, 1],  #
            [pi, 1, 0, 0],  # twofold about  2 -1  0 (x1,a1)
            [pi, -0.5, sq3by2, 0],  # twofold about -1  2  0 (a2)
            [pi, -0.5, -sq3by2, 0],  # twofold about -1 -1  0 (a3)
            [pi, sq3by2, 0.5, 0],  # twofold about  1  0  0
            [pi, 0, 1, 0],  # twofold about -1  1  0 (x2)
            [pi, -sq3by2, 0.5, 0],  # twofold about  0 -1  0
        ]
    elif tag.lower() == 'th':
        # CUBIC (LOW)
        angleAxis = c_[
            [0.0, 1, 0, 0],  # identity
            [pi, 1, 0, 0],  # twofold about    1  0  0 (x1)
            [pi, 0, 1, 0],  # twofold about    0  1  0 (x2)
            [pi, 0, 0, 1],  # twofold about    0  0  1 (x3)
            [piby3 * 2, 1, 1, 1],  # threefold about  1  1  1
            [piby3 * 4, 1, 1, 1],  #
            [piby3 * 2, -1, 1, 1],  # threefold about -1  1  1
            [piby3 * 4, -1, 1, 1],  #
            [piby3 * 2, -1, -1, 1],  # threefold about -1 -1  1
            [piby3 * 4, -1, -1, 1],  #
            [piby3 * 2, 1, -1, 1],  # threefold about  1 -1  1
            [piby3 * 4, 1, -1, 1],  #
        ]
    elif tag.lower() == 'oh':
        # CUBIC (HIGH)
        angleAxis = c_[
            [0.0, 1, 0, 0],  # identity
            [piby2, 1, 0, 0],  # fourfold about   1  0  0 (x1)
            [pi, 1, 0, 0],  #
            [piby2 * 3, 1, 0, 0],  #
            [piby2, 0, 1, 0],  # fourfold about   0  1  0 (x2)
            [pi, 0, 1, 0],  #
            [piby2 * 3, 0, 1, 0],  #
            [piby2, 0, 0, 1],  # fourfold about   0  0  1 (x3)
            [pi, 0, 0, 1],  #
            [piby2 * 3, 0, 0, 1],  #
            [piby3 * 2, 1, 1, 1],  # threefold about  1  1  1
            [piby3 * 4, 1, 1, 1],  #
            [piby3 * 2, -1, 1, 1],  # threefold about -1  1  1
            [piby3 * 4, -1, 1, 1],  #
            [piby3 * 2, -1, -1, 1],  # threefold about -1 -1  1
            [piby3 * 4, -1, -1, 1],  #
            [piby3 * 2, 1, -1, 1],  # threefold about  1 -1  1
            [piby3 * 4, 1, -1, 1],  #
            [pi, 1, 1, 0],  # twofold about    1  1  0
            [pi, -1, 1, 0],  # twofold about   -1  1  0
            [pi, 1, 0, 1],  # twofold about    1  0  1
            [pi, 0, 1, 1],  # twofold about    0  1  1
            [pi, -1, 0, 1],  # twofold about   -1  0  1
            [pi, 0, -1, 1],  # twofold about    0 -1  1
        ]
    else:
        raise RuntimeError(
            "unrecognized symmetry group.  "
            + "See ``help(quatOfLaueGroup)'' for a list of valid options.  "
            + "Oh, and have a great day ;-)"
        )

    angle = angleAxis[0,]
    axis = angleAxis[1:,]

    #  Note: Axis does not need to be normalized in call to quatOfAngleAxis
    #  05/01/2014 JVB -- made output a contiguous C-ordered array
    qsym = array(quatOfAngleAxis(angle, axis).T, order='C').T

    return qsym
