"""HDF5 adapter class
"""
import h5py
import warnings

import numpy as np

from . import ImageSeriesAdapter
from ..imageseriesiter import ImageSeriesIterator


class HDF5ImageSeriesAdapter(ImageSeriesAdapter):
    """collection of images in HDF5 format"""

    format = 'hdf5'

    def __init__(self, fname, **kwargs):
        """Constructor for H5FrameSeries

        *fname* - filename of the HDF5 file, or an open h5py file
                  (this class will close the h5py file when finished)
        *kwargs* - keyword arguments, choices are:
           path - (required) path of dataset in HDF5 file
        """
        if isinstance(fname, h5py.File):
            self.__h5name = fname.filename
            self.__h5file = fname
        else:
            self.__h5name = fname
            self.__h5file = h5py.File(self.__h5name, 'r')

        self.__path = kwargs['path']
        self.__dataname = kwargs.pop('dataname', 'images')
        self.__images = '/'.join([self.__path, self.__dataname])
        self._load_data()
        self._meta = self._getmeta()

    def close(self):
        self.__image_dataset = None
        self.__data_group = None
        self.__h5file.close()
        self.__h5file = None

    def __del__(self):
        # !!! Note this is not ideal, as the use of __del__ is problematic.
        #     However, it is highly unlikely that the usage of a ImageSeries
        #     would pose a problem.  A warning will (hopefully) be emitted if
        #     an issue arises at some point
        try:
            self.close()
        except(Exception):
            warnings.warn("HDF5ImageSeries could not close h5file")
            pass

    def __getitem__(self, key):
        return self.__image_dataset[key]

    def __iter__(self):
        return ImageSeriesIterator(self)

    def __len__(self):
        return len(self.__image_dataset)

    def __getstate__(self):
        # Remove any non-pickleable attributes
        to_remove = [
            '__h5file',
            '__image_dataset',
            '__data_group',
        ]

        # Prefix them with the private prefix
        prefix = f'_{self.__class__.__name__}'
        to_remove = [f'{prefix}{x}' for x in to_remove]

        # Make a copy of the dict to modify
        state = self.__dict__.copy()

        # Remove them
        for attr in to_remove:
            state.pop(attr)

        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.__h5file = h5py.File(self.__h5name, 'r')
        self._load_data()

    def _load_data(self):
        data = np.asarray(self.__h5file[self.__images])
        if data.ndim == 2:
            self.__image_dataset = np.dstack(data)
        elif data.ndim == 3:
            self.__image_dataset = data
        else:
            raise RuntimeError(
                f'Image data must be a 2-d or 3-d array; yours is {data.ndim}'
            )
        self.__data_group = self.__h5file[self.__path]

    def _getmeta(self):
        mdict = {}
        for k, v in list(self.__data_group.attrs.items()):
            mdict[k] = v

        return mdict

    @property
    def metadata(self):
        """(read-only) Image sequence metadata

        note: metadata loaded on open and allowed to be modified
        """
        return self._meta

    @property
    def dtype(self):
        return self.__image_dataset.dtype

    @property
    def shape(self):
        return self.__image_dataset.shape[1:]

    pass  # end class
