from __future__ import division

import os.path
import os
import logging
import tempfile
from abc import ABCMeta, abstractmethod
from collections import OrderedDict, namedtuple
import json
import pickle
import itertools

import matplotlib.pyplot as plt
import rasterio
import rasterio.mask
from rasterio.warp import reproject
from affine import Affine
import numpy as np
import shapefile
import pyproj
import matplotlib.pyplot as plt

from uncoverml import mpiops
from uncoverml import image
from uncoverml import features
from uncoverml import diagnostics
from uncoverml.transforms import missing_percentage
from uncoverml.targets import Targets


_logger = logging.getLogger(__name__)


_lower_is_better = ['mll', 'mll_transformed', 'smse', 'smse_transformed']

SharedTrainingData = namedtuple(
        'TrainingData', ['targets_all', 'x_all', 'obs_win', 'pos_win', 'field_wins', 'x_win'])

class ImageSource:
    __metaclass__ = ABCMeta

    @abstractmethod
    def data(self, min_x, max_x, min_y, max_y):
        pass

    @property
    def full_resolution(self):
        return self._full_res

    @property
    def dtype(self):
        return self._dtype

    @property
    def nodata_value(self):
        return self._nodata_value

    @property
    def pixsize_x(self):
        return self._pixsize_x

    @property
    def pixsize_y(self):
        return self._pixsize_y

    @property
    def origin_latitude(self):
        return self._start_lat

    @property
    def origin_longitude(self):
        return self._start_lon

    @property
    def crs(self):
        return self._crs


class RasterioImageSource(ImageSource):

    def __init__(self, filename):

        self._filename = filename
        assert os.path.isfile(filename), '{} does not exist'.format(filename)
        with rasterio.open(self._filename, 'r') as geotiff:
            self._full_res = (geotiff.width, geotiff.height, geotiff.count)
            self._nodata_value = geotiff.meta['nodata']
            # we don't support different channels with different dtypes
            for d in geotiff.dtypes[1:]:
                if geotiff.dtypes[0] != d:
                    raise ValueError("No support for multichannel geotiffs "
                                     "with differently typed channels")
            self._dtype = np.dtype(geotiff.dtypes[0])
            self._crs = geotiff.crs

            A = geotiff.transform
            # No shearing or rotation allowed!!
            if not ((A[1] == 0) and (A[3] == 0)):
                raise RuntimeError("Transform to pixel coordinates"
                                   "has rotation or shear")
            self._pixsize_x = A[0]
            self._pixsize_y = A[4]
            self._start_lon = A[2]
            self._start_lat = A[5]

            self._y_flipped = self._pixsize_y < 0
            if self._y_flipped:
                self._start_lat += self._pixsize_y * self._full_res[1]
                self._pixsize_y *= -1

    def data(self, min_x, max_x, min_y, max_y):

        if self._y_flipped:
            min_y_new = self._full_res[1] - max_y
            max_y_new = self._full_res[1] - min_y
            min_y = min_y_new
            max_y = max_y_new

        # NOTE these are exclusive
        window = ((min_y, max_y), (min_x, max_x))
        with rasterio.open(self._filename, 'r') as geotiff:
            d = geotiff.read(window=window, masked=True)
        d = d[np.newaxis, :, :] if d.ndim == 2 else d
        d = np.ma.transpose(d, [2, 1, 0])  # Transpose and channels at back

        # if nans exist in data, mask them, i.e. convert to nodatavalue
        # TODO: Consider removal once covariates are fixed
        nans = np.isnan(d.data)
        if d.mask.ndim:
            d.mask[nans] = True

        if self._y_flipped:
            d = d[:, ::-1]

        # Otherwise scikit image complains
        m = np.ma.MaskedArray(data=np.ascontiguousarray(d.data),
                              mask=np.ascontiguousarray(d.mask))

        # # uniform mask format
        # if np.ma.count_masked(m) == 0:
        #     m = np.ma.masked_array(data=m.data,
        #                            mask=np.zeros_like(m.data, dtype=bool))
        assert m.data.ndim == 3
        assert m.mask.ndim == 3 or m.mask.ndim == 0
        return m


class ArrayImageSource(ImageSource):
    """
    An image source that uses an internally stored numpy array

    Parameters
    ----------
    A : MaskedArray
        masked array of shape (xpix, ypix, channels) that contains the
        image data.
    origin : ndarray
        Array of the form [lonmin, latmin] that defines the origin of the image
    pixsize : ndarray
        Array of the form [pixsize_x, pixsize_y] defining the size of a pixel
    """
    def __init__(self, A, origin, crs, pixsize):
        self._data = A
        self._full_res = A.shape
        self._dtype = A.dtype
        self._nodata_value = A.fill_value
        self._pixsize_x = pixsize[0]
        self._pixsize_y = pixsize[1]
        self._start_lon = origin[0]
        self._start_lat = origin[1]
        self._crs = crs

    def data(self, min_x, max_x, min_y, max_y):
        # MUST BE EXCLUSIVE
        data_window = self._data[min_x:max_x, :][:, min_y:max_y]
        return data_window

def crop_covariates(config, outdir=None):
    """
    Crops the covariate files listed under `config.feature_sets` using
    the bounds provided under `config.extents`. The cropped covariates
    are stored in a temporary directory and the paths in 
    `config.feature_sets` are redirected to theses files. The caller is
    responsible for removing the files once they have been created.
    
    Parameters
    ----------
    config : `uncoverml.config.Config`
        Parsed UncoverML config.
    outdir : str
        Aboslute path to directory to store cropped covariates.
        If not provided, a tmp directory will be created.
    """
    _logger.info("Cropping covariates...")
    if outdir is None:
        config.tmpdir = tempfile.mkdtemp()
        def _new_fname(fname):
            return os.path.join(config.tmpdir, os.path.basename(fname))
    else:
        def _new_fname(fname):
            return None

    for s in config.feature_sets:
        proc_files = np.array_split(s.files, mpiops.chunks)[mpiops.chunk_index]
        new_files = [crop_tif(f, config.extents, _new_fname(f)) for f in proc_files]
        new_files = mpiops.comm.allgather(new_files)
        mpiops.comm.barrier()
        s.files = list(itertools.chain(*new_files))

def crop_tif(filename, extents, outfile=None):
    """
    Crops the geotiff using the provided extent.
    
    Parameters
    ----------
    filename : str
        Path to the geotiff to be cropped.
    extents : tuple(float, float, float, float)
        Bounding box to crop by, ordering is (xmin, ymin, xmax, ymax).
        Data outside bounds will be cropped. Any elements that are None
        will be substituted with the original bound of the geotiff.
    outfile : str
        Path to save cropped geotiff. If not provided, will be saved
        with original name + random id in tmp directory.
    """
    with rasterio.open(filename) as src:
        if any(c is None for c in extents):
            extents = \
                tuple(src.bounds[i] if extents[i] is None else extents[i] for i in range(4))
        xmin, ymin, xmax, ymax = extents

        def _check_bound(comp, crop_bnd, src_bnd, s):
            if comp:
                raise ValueError(f"Crop coordinate '{s}' ({crop_bnd}) is out of bounds of image ({src_bnd})")

        _check_bound(xmin < src.bounds[0], xmin, src.bounds[0], 'xmin')
        _check_bound(ymin < src.bounds[1], ymin, src.bounds[1], 'ymin')
        _check_bound(xmax > src.bounds[2], xmax, src.bounds[2], 'xmax')
        _check_bound(ymax > src.bounds[3], ymax, src.bounds[3], 'ymax')

        gj_box = [
            {'type': 'Polygon',
            'coordinates': [[
                (xmin, ymin), (xmin, ymax), 
                (xmax, ymax), (xmax, ymin),(xmin, ymin)]]}
        ]        
        out_image, out_transform = rasterio.mask.mask(src, gj_box, crop=True)
        out_meta = src.meta
        out_meta.update({"driver": "GTiff", 
                         "height": out_image.shape[1],
                         "width": out_image.shape[2],
                         "transform": out_transform})

        if outfile is None:
            prefix, suffix = os.path.splitext(os.path.basename(filename))
            fd, outfile = tempfile.mkstemp(suffix, prefix)
            # Close file descriptor because we don't need it
            os.close(fd)

        with rasterio.open(outfile, "w", **out_meta) as dest:
            dest.write(out_image)

        return outfile

def number_of_targets(filename):
    """
    Convenience function for getting number of targets in shapefile.
    Useful for generating an equal number of points on the root 
    processor when it only has a chunk of the real targets and so 
    doesn't know how many targets there are.

    Parameters
    ----------
    filename : str 
        Path to targets shapefile.

    Returns
    -------
    int 
        Number of records in the shapefile.
    """
    sf = shapefile.Reader(filename)
    return sf.numRecords

def load_shapefile(filename, targetfield, covariate_crs, extents):
    """
    TODO
    """
    sf = shapefile.Reader(filename)
    if extents and any(c is None for c in extents):
        extents = tuple(sf.bbox[i] if extents[i] is None else extents[i] for i in range(4))
    shapefields = [f[0] for f in sf.fields[1:]]  # Skip DeletionFlag
    dtype_flags = [(f[1], f[2]) for f in sf.fields[1:]]  # Skip DeletionFlag
    dtypes = ['float' if (k[0] == 'N' or k[0] == 'F') else '<U{}'.format(k[1])
              for k in dtype_flags]
    records = np.array(sf.records()).T
    record_dict = {k: np.array(r, dtype=d) for k, r, d in zip(
        shapefields, records, dtypes)}
    if targetfield is None:
        targetfield = list(record_dict.keys())[0]
    if targetfield in record_dict:
        val = record_dict.pop(targetfield)
    else:
        raise ValueError("Can't find target property in shapefile." +
                         "Candidates: {}".format(record_dict.keys()))
    othervals = record_dict

    # Try to get CRS.
    src_prj, dst_prj = None, None
    if not covariate_crs:
        _logger.warning("Could not get covariate CRS for reprojecting target shapefile. Ensure the "
                    "target shapefile is in the same projection as the covariates or errors will "
                    "occur.")
    else:
        prj_file = os.path.splitext(filename)[0] + '.prj'
        if os.path.exists(prj_file):
            with open(prj_file, 'r') as f:
                wkt = f.readline()
            if pyproj.crs.is_wkt(wkt):
                src_prj = pyproj.Proj(pyproj.CRS(wkt))
                if src_prj.crs.to_epsg() != covariate_crs.to_epsg():
                    src_prj = src_prj.crs.to_epsg()
                    dst_prj = covariate_crs.to_epsg()
            else:
                _logger.warning("Found a '.prj' file for target shapefile but text contained is not "
                            "in 'wkt' format. Continuing without reprojecting.")
        else:
            _logger.warning("Could not find any '.prj' file for target shapefile. Ensure the target "
                        "shapefile is in same projection as the covariates or errors will occur.") 
        
    # Get coordinates
    coords = []
    for shape in sf.iterShapes():
        coords.append(list(shape.__geo_interface__['coordinates']))
    label_coords = np.array(coords).squeeze()
    if src_prj and dst_prj:
        label_coords = np.array([coord for coord in pyproj.itransform(src_prj, dst_prj, 
                                                                      label_coords, always_xy=True)])
    if extents:
        def _in_extents(coord):
            return extents[0] <= coord[0] <= extents[2] \
                    and extents[1] <= coord[1] <= extents[3]
        label_coords = np.array([coord for coord in label_coords if _in_extents(coord)])
    return label_coords, val, othervals


def load_targets(shapefile, targetfield=None, covariate_crs=None, extents=None):
    """
    Loads the shapefile onto node 0 then distributes it across all
    available nodes
    """
    if mpiops.chunk_index == 0:
        lonlat, vals, othervals = load_shapefile(shapefile, targetfield, covariate_crs, extents)
        # sort by y then x
        ordind = np.lexsort(lonlat.T)
        vals = vals[ordind]
        lonlat = lonlat[ordind]
        for k, v in othervals.items():
            othervals[k] = v[ordind]

        lonlat = np.array_split(lonlat, mpiops.chunks)
        vals = np.array_split(vals, mpiops.chunks)
        split_othervals = {k: np.array_split(v, mpiops.chunks)
                           for k, v in othervals.items()}
        othervals = [{k: v[i] for k, v in split_othervals.items()}
                     for i in range(mpiops.chunks)]
    else:
        lonlat, vals, othervals = None, None, None

    lonlat = mpiops.comm.scatter(lonlat, root=0)
    vals = mpiops.comm.scatter(vals, root=0)
    othervals = mpiops.comm.scatter(othervals, root=0)
    _logger.info(":mpi:Assigned {} targets".format(lonlat.shape[0]))
    targets = Targets(lonlat, vals, othervals=othervals)
    return targets

def get_image_crs(config):
    image_file = config.feature_sets[0].files[0]
    im = image.Image(RasterioImageSource(image_file))
    return im.crs 

def get_image_bounds(config):
    image_file = config.feature_sets[0].files[0]
    im = image.Image(RasterioImageSource(image_file))
    return im.bbox
     
def get_image_spec(model, config):
    # temp workaround, we should have an image spec to check against
    nchannels = len(model.get_predict_tags())
    imagelike = config.feature_sets[0].files[0]
    template_image = image.Image(RasterioImageSource(imagelike))
    eff_shape = template_image.patched_shape(config.patchsize) + (nchannels,)
    eff_bbox = template_image.patched_bbox(config.patchsize)
    crs = template_image.crs
    return eff_shape, eff_bbox, crs


class ImageWriter:

    nodata_value = np.array(-1e20, dtype='float32')

    def __init__(self, shape, bbox, crs, n_subchunks, outpath,
                 outbands, band_tags=None, independent=False, **kwargs):
        """
        pass in additional geotif write options in kwargs
        """
        # affine
        self.A, _, _ = image.bbox2affine(bbox[1, 0], bbox[0, 0],
                                         bbox[0, 1], bbox[1, 1],
                                         shape[0], shape[1])
        self.shape = shape
        self.outbands = outbands
        self.bbox = bbox
        self.outpath = outpath
        self.n_subchunks = n_subchunks
        self.independent = independent  # mpi control
        self.sub_starts = [k[0] for k in np.array_split(
                           np.arange(self.shape[1]),
                           mpiops.chunks * self.n_subchunks)]

        # file tags don't have spaces
        if band_tags:
            if self.outbands > len(band_tags):
                _logger.warning(f"Specified more outbands ({self.outbands}) than there are "
                                f"prediction tags available ({len(band_tags)}). "
                                f"Limiting outbands to number of prediction tags.")
                self.outbands = len(band_tags)
            file_tags = ["_".join(k.lower().split()) for k in band_tags]
        else:
            file_tags = [str(k) for k in range(self.outbands)]
            band_tags = file_tags

        files = []
        file_names = []

        if mpiops.chunk_index == 0:
            for band in range(self.outbands):
                output_filename = self.outpath.format(file_tags[band])
                f = rasterio.open(output_filename, 'w', driver='GTiff',
                                  width=self.shape[0], height=self.shape[1],
                                  dtype=np.float32, count=1,
                                  crs=crs,
                                  transform=self.A,
                                  nodata=self.nodata_value,
                                  **kwargs
                                  )
                f.update_tags(1, image_type=band_tags[band])
                files.append(f)
                file_names.append(output_filename)

        if independent:
            self.files = files
        else:
            if mpiops.chunk_index == 0:
                # create a file for each band
                self.files = files
                self.file_names = file_names
            else:
                self.file_names = []

            self.file_names = mpiops.comm.bcast(self.file_names, root=0)

    def write(self, x, subchunk_index):
        """
        :param x:
        :param subchunk_index:
        :param independent: bool
            independent image writing by different processes, i.e., images are not chunked
        :return:
        """
        x = x.astype(np.float32)
        rows = self.shape[0]
        bands = x.shape[1]

        image = x.reshape((rows, -1, bands))
        # make sure we're writing nodatavals
        if x.mask is not False:
            x.data[x.mask] = self.nodata_value

        mpiops.comm.barrier()
        _logger.info("Writing partition to output file")

        if self.independent:
            data = np.ma.transpose(image, [2, 1, 0])  # untranspose
            # write each band separately
            for i, f in enumerate(self.files):
                f.write(data[i:i+1])
        else:
            if mpiops.chunk_index != 0:
                mpiops.comm.send(image, dest=0)
            else:
                for node in range(mpiops.chunks):
                    node = mpiops.chunks - node - 1
                    subindex = mpiops.chunks*subchunk_index + node
                    ystart = self.sub_starts[subindex]
                    data = mpiops.comm.recv(source=node) \
                        if node != 0 else image
                    data = np.ma.transpose(data, [2, 1, 0])  # untranspose
                    yend = ystart + data.shape[1]  # this is Y
                    window = ((ystart, yend), (0, self.shape[0]))
                    # write each band separately
                    for i, f in enumerate(self.files):
                        f.write(data[i:i+1], window=window)

        mpiops.comm.barrier()

    def close(self):  # we can explicitly close rasters using this
        if mpiops.chunk_index == 0:
            for f in self.files:
                f.close()
        mpiops.comm.barrier()

    def output_thumbnails(self, ratio=10):
        this_chunk_files = np.array_split(self.file_names,
                                          mpiops.chunks)[mpiops.chunk_index]
        for f in this_chunk_files:
            thumbnails = os.path.splitext(f)
            thumbnail = thumbnails[0] + '_thumbnail' + thumbnails[1]
            resample(f, output_tif=thumbnail, ratio=ratio)


def feature_names(config):

    results = []
    for s in config.feature_sets:
        feats = []
        for tif in s.files:
            name = os.path.basename(tif)
            feats.append(name)
        feats.sort()
        results += feats
    return results


def _iterate_sources(f, config):

    results = []
    for s in config.feature_sets:
        extracted_chunks = {}
        for tif in s.files:
            name = os.path.basename(tif)
            image_source = RasterioImageSource(tif)
            x = f(image_source)
            # TODO this may hurt performance. Consider removal
            if type(x) is np.ma.MaskedArray:
                count = mpiops.count(x)
                # if not np.all(count > 0):
                #     s = ("{} has no data in at least one band.".format(name) +
                #          " Valid_pixel_count: {}".format(count))
                #     raise ValueError(s)
                missing_percent = missing_percentage(x)
                t_missing = mpiops.comm.allreduce(
                    missing_percent) / mpiops.chunks
                _logger.info("{}: {}px {:2.2f}% missing".format(
                    name, count, t_missing))
            extracted_chunks[name] = x
        extracted_chunks = OrderedDict(sorted(
            extracted_chunks.items(), key=lambda t: t[0]))

        results.append(extracted_chunks)
    return results


def image_resolutions(config):
    def f(image_source):
        r = image_source._full_res
        return r

    result = _iterate_sources(f, config)
    return result


def image_subchunks(subchunk_index, config):

    def f(image_source):
        r = features.extract_subchunks(image_source, subchunk_index,
                                       config.n_subchunks, config.patchsize)
        return r
    result = _iterate_sources(f, config)
    return result


def image_feature_sets(targets, config):

    def f(image_source):
        r = features.extract_features(image_source, targets,
                                      config.n_subchunks, config.patchsize)
        return r
    result = _iterate_sources(f, config)
    return result


def semisupervised_feature_sets(targets, config):

    frac = config.subsample_fraction

    def f(image_source):
        r_t = features.extract_features(image_source, targets, n_subchunks=1,
                                        patchsize=config.patchsize)
        r_a = features.extract_subchunks(image_source, subchunk_index=0,
                                         n_subchunks=1,
                                         patchsize=config.patchsize)
        if frac < 1.0:
            np.random.seed(1)
            r_a = r_a[np.random.rand(r_a.shape[0]) < frac]

        r_data = np.concatenate([r_t.data, r_a.data], axis=0)
        r_mask = np.concatenate([r_t.mask, r_a.mask], axis=0)
        r = np.ma.masked_array(data=r_data, mask=r_mask)
        return r
    result = _iterate_sources(f, config)
    return result

def unsupervised_feature_sets(config):
    frac = config.subsample_fraction

    def f(image_source):
        r = features.extract_subchunks(image_source, subchunk_index=0,
                                       n_subchunks=1,
                                       patchsize=config.patchsize)
        if frac < 1.0:
            np.random.seed(1)
            r = r[np.random.rand(r.shape[0]) < frac]
        return r
    result = _iterate_sources(f, config)
    return result

def export_feature_ranks(measures, feats, scores, config):
    score_listing = dict(scores={}, ranks={})

    for measure, measure_scores in zip(measures, scores):
        # Sort the scores
        scores = sorted(zip(feats, measure_scores),
                        key=lambda s: s[1])
        if measure in _lower_is_better:
            scores.reverse()
        sorted_features, sorted_scores = zip(*scores)

        # Store the results
        score_listing['scores'][measure] = sorted_scores
        score_listing['ranks'][measure] = sorted_features

    # Write the results out to a file
    with open(config.feature_ranks_file, 'w') as output_file:
        json.dump(score_listing, output_file, sort_keys=True, indent=4)

    if config.plot_feature_ranks:
        diagnostics.plot_feature_ranks(
            config.feature_ranks_file).savefig(config.plot_feature_ranks)
        diagnostics.plot_feature_rank_curves(
            config.feature_ranks_file).savefig(config.plot_feature_rank_curves)

def export_model(model, config):
    with open(config.model_file, 'wb') as f:
        pickle.dump(model, f)

def _make_valid_array_name(label):
    label = "_".join(label.split())
    label = ''.join(filter(str.isalnum, label))  # alphanum only
    if label[0].isdigit():
        label = '_' + label
    return label

def resample(input_tif, output_tif, ratio, resampling=5):
    """
    Parameters
    ----------
    input_tif: str or rasterio.io.DatasetReader
        input file path or rasterio.io.DatasetReader object
    output_tif: str
        output file path
    ratio: float
        ratio by which to shrink/expand
        ratio > 1 means shrink
    resampling: int, optional
        default is 5 (average) resampling. Other options are as follows:
        nearest = 0
        bilinear = 1
        cubic = 2
        cubic_spline = 3
        lanczos = 4
        average = 5
        mode = 6
        gauss = 7
        max = 8
        min = 9
        med = 10
        q1 = 11
        q3 = 12
    """
    src = rasterio.open(input_tif, mode='r')

    nodatavals = src.get_nodatavals()
    new_shape = round(src.height / ratio), round(src.width / ratio)
    # adjust the new affine transform to the smaller cell size
    aff = Affine.from_gdal(*src.get_transform())
    newaff = aff * Affine.scale(ratio)

    dest = rasterio.open(output_tif, 'w', driver='GTiff',
                         height=new_shape[0], width=new_shape[1],
                         count=src.count, dtype=rasterio.float32,
                         crs=src.crs, transform=newaff,
                         nodata=nodatavals[0])

    for b in range(src.count):
        arr = src.read(b+1)
        new_arr = np.empty(shape=new_shape, dtype=arr.dtype)
        reproject(arr, new_arr,
                  src_transform=aff,
                  dst_transform=newaff,
                  src_crs=src.crs,
                  src_nodata=nodatavals[b],
                  dst_crs=src.crs,
                  dst_nodata=nodatavals[b],
                  resample=resampling)
        dest.write(new_arr, b + 1)
    src.close()
    dest.close()

SharedTrainingData = namedtuple(
        'TrainingData', ['targets_all', 'x_all', 'obs_win', 'pos_win', 'field_wins', 'x_win'])

def create_shared_training_data(targets_all, x_all):
    x_all, x_win = mpiops.create_shared_array(x_all)

    if targets_all is None:
        targets_all = Targets(None, None, None)
    targets_all.observations, obs_win = mpiops.create_shared_array(targets_all.observations)
    targets_all.positions, pos_win = mpiops.create_shared_array(targets_all.positions)

    field_keys = list(targets_all.fields.keys()) if mpiops.chunk_index == 0 else None
    field_keys = mpiops.comm.bcast(field_keys, root=0)

    field_wins = []
    for k in field_keys:
        v = targets_all.fields[k] if mpiops.chunk_index == 0 else None
        shared_v, field_win = mpiops.create_shared_array(v)
        targets_all.fields[k] = shared_v
        field_wins.append(field_win)

    return SharedTrainingData(targets_all, x_all, obs_win, pos_win, field_wins, x_win)

def deallocate_shared_training_data(training_data):
    training_data.obs_win.Free()
    training_data.pos_win.Free()
    training_data.x_win.Free()
    for win in training_data.field_wins:
        win.Free()
