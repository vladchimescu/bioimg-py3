#!/usr/bin/env python3
import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from skimage.util import view_as_blocks, view_as_windows
from skimage.util import img_as_ubyte


def tile_images(imgs, tile_size):
    '''Tile images
       --------------------------------------
       Grid images into blocks of specified width
       and height and return the list of image tiles.
       Supports greyscale, color and 3D greyscale images

       Parameters
       ----------
       imgs : list or array of images
       tile_size : tuple
       
       Returns
       -------
       list : list of tiled images
    '''
    height, width = tile_size
    # if greyscale image
    if imgs[0].ndim == 2:
        h, w = imgs[0].shape
        # clip if the tile size doesn't match image height
        if h % height != 0:
            imgs = [img[0:-(h % height),:] for img in imgs]
        # clip if the tile size doesn't match image width
        if w % width != 0:
            imgs = [img[:,0:-(w % width)] for img in imgs]
        block_shape = (height, width)
    # if multichannel or 3D image
    if imgs[0].ndim == 3:
        h, w, nchan = imgs[0].shape
        # clip if the tile size doesn't match image height
        if h % height != 0:
            imgs = [img[0:-(h % height),:,:] for img in imgs]
        # clip if the tile size doesn't match image width
        if w % width != 0:
            imgs = [img[:,0:-(w % width),:] for img in imgs]
        block_shape = (height, width, nchan)
    if imgs[0].ndim > 3:
        raise TypeError("Only 2D and 3D image arrays are supported")
    return [view_as_blocks(img, block_shape=block_shape).reshape(-1, *block_shape) for img in imgs]

def get_block_counts(a):
    '''Counts frequency of each element in an image block
       --------------------------------------------------
       Computes occurence frequency of block elements,
       such as pixel values (for int-valued images) or
       block types in a superblock

       Parameters
       ----------
       a : array
           Image block

       Returns
       -------
       df : DataFrame
           DataFrame with frequencies of each element.
           Columns indicate bins / levels
    '''
    block_type = dict(zip(*np.unique(a, return_counts=True)))
    return pd.DataFrame([block_type]) / a.size

def get_blockfeats(blocks):
    '''Compute features for greyscale image blocks
       -------------------------------------------
       For greyscale blocks, the features are 
       occurence frequencies of individual bits
       (images are assumed to be of 8 or 16-bit integer type)
    
       Parameters
       ----------
       blocks : list-like
           List or array of greyscale image blocks

       Returns
       -------
       df : DataFrame
           DataFrame with blocks in rows and
           block features in columns
    '''
    mask = np.array([(bl != 0).sum() > 0.5 * bl.size for bl in blocks])
    blockfeats = pd.concat([get_block_counts(bl) for bl in blocks[mask]])
    blockfeats.index = np.where(mask)[0]
    return blockfeats

def get_block_types(bf, km_block, cols, grid_shape):
    '''Cluster greyscale image blocks based on features
       ------------------------------------------------
       Use the pre-trained KMeans object to return
       cluster labels of each block in a greyscale image

       Parameters
       ----------
       bf : DataFrame with block features
           Each block is characterized by bit frequencies
       km_block : KMeans object
           KMeans model trained on greyscale image blocks
       cols : array
           greyscale bit levels
       grid_shape : tuple
           Number of blocks in rows and columns

       Returns
       -------
       img_blocked : array
           Array (matrix) with block types. The spatial
           order (as in the original image) is preserved
    '''
    img_blocked = np.zeros(grid_shape[0] * grid_shape[1])
    # make sure has the same columns as all other blocks
    bf = bf.reindex(columns=cols).fillna(0)
    # only if index is set (foreground blocks)
    img_blocked[bf.index] = km_block.predict(bf) + 1
    img_blocked = img_blocked.reshape(grid_shape)
    return img_blocked

def get_supblocks(img_blocked, window_shape=3):
    '''Computes superblock features for greyscale images
       -------------------------------------------------

       Parameters
       ----------
       img_blocked : array
           Grid (matrix) with block types
           (Block types are integers in range 1 ... n_block_types)
       window_shape : int (optional)
           Size of a sliding window, by default 3x3 window is used

       Returns
       -------
       df : DataFrame
           DataFrame with superblocks in rows and features
           in columns. Superblock features for greyscale images
           are simply block type occurence frequencies
    '''
    supblocks = view_as_windows(img_blocked,
                                window_shape=window_shape).reshape(-1,window_shape,
                                                                   window_shape)
    mid = np.ceil(window_shape/2).astype(int) - 1
    fgr_supblocks = np.stack([sb for sb in supblocks if sb[mid,mid]])
    return pd.concat([get_block_counts(sb) for sb in fgr_supblocks])

def get_color_supblocks(img, window_shape=3):
    supblocks = view_as_windows(img,
                                    window_shape=window_shape).reshape(-1,window_shape, window_shape)
    return pd.concat([get_block_counts(sb) for sb in supblocks])

def flatten_tiles(blocks):
    return np.array([block.ravel() for block in blocks])

class SegfreeProfiler:
    def __init__(self, **kwargs):
        '''
        Segmentation-free profiler class
        --------------------------------
        Generates segmentation-free profiles for multichannel or 3D images

        Attributes
        ----------
        tile_size : tuple
        n_block_types : int
        n_supblock_types : int
        n_components : int
        n_subset : int
        pca : PCA object for dimensionality reduction
        km_block : KMeans object for tiles (blocks)
        km_supblock : KMeans object for superblocks

        Methods
        -------
        fit(imgs, n_init=50, random_state=1307)
        fit_transform(imgs, n_init=50, random_state=1307)
        transform(imgs)
        
        '''
        self.tile_size = kwargs.get('tile_size', None)
        self.n_block_types = kwargs.get('n_block_types', 50)
        self.n_supblock_types = kwargs.get('n_supblock_types', 30)
        self.n_components = kwargs.get('n_components', 50)
        self.n_subset = kwargs.get('n_subset', 10000)
        
        # these are initialized with 'None'
        self.pca = None
        self.km_block = None
        self.km_supblock = None
        self.pixel_types = None

    def __setattr__(self, name, value):
        self.__dict__[name] = value

    def set_param(self, **kwargs):
        for k in kwargs.keys():
            self.__setattr__(k, kwargs[k])

    def _fit_single_channel(self, imgs, n_init,
                            random_state,
                            transform=False):
        img_tiles = tile_images(imgs, self.tile_size)
        print("Estimating tile properties")
        blockfeats = [get_blockfeats(t) for t in img_tiles]
        blockdf = pd.concat(blockfeats).fillna(0)
        self.pixel_types = blockdf.columns.values
        print("Running k-means on tiles")
        self.km_block = KMeans(n_clusters=self.n_block_types,
                               n_init=n_init,
                               random_state=random_state).fit(blockdf)
        
        grid_shape = tuple(int(x / y) for x,y in zip(imgs[0].shape, self.tile_size))
        blocks = [get_block_types(bf,
                              km_block=self.km_block,
                              cols=self.pixel_types,
                              grid_shape=grid_shape) for bf in blockfeats]
        supblocks = [get_supblocks(bl) for bl in blocks]
        print("Running k-means on superblocks")
        self.km_supblock = KMeans(n_clusters=self.n_supblock_types,
                                  n_init=n_init,
                                  random_state=random_state).fit(pd.concat(supblocks).fillna(0))
        if transform:
            return self._transform_single_channel(imgs, blocks, supblocks)
        print("Done")

    def _fit_multichannel(self, imgs, n_init,
                          random_state,
                          transform=False):
        img_tiles = tile_images(imgs, self.tile_size)
        Xtrain = np.concatenate([flatten_tiles(t) for t in img_tiles])
        print("Running PCA on tiles")
        self.pca = PCA(n_components=self.n_components,
                       svd_solver='randomized',
                       whiten=True,
                       random_state=random_state).fit(Xtrain)
        blockdf = self.pca.transform(Xtrain)
        np.random.seed(random_state)
        subset = np.random.choice(range(blockdf.shape[0]), size=self.n_subset)
        print("Running k-means on tiles")
        self.km_block = KMeans(n_clusters=self.n_block_types,
                               n_init=n_init,
                               random_state=random_state).fit(blockdf[subset,:])

        grid_shape = tuple(int(x / y) for x,y in zip(imgs[0].shape, self.tile_size))
        img_blocked = self.km_block.predict(blockdf).reshape(-1, *grid_shape)
        supblocks = [get_color_supblocks(img_blocked[i]) for i in range(img_blocked.shape[0])]
        print("Running k-means on superblocks")
        self.km_supblock = KMeans(n_clusters=self.n_supblock_types,
                                  n_init=n_init,
                                  random_state=random_state).fit(pd.concat(supblocks).fillna(0))
        if transform:
            return self._transform_multichannel(imgs, img_blocked, supblocks)
        print("Done")
        
        

    def fit(self, imgs, n_init=50, random_state=1307):
        if imgs[0].ndim == 2:
            print("Fitting model for greyscale images")
            self._fit_single_channel(imgs=imgs, n_init=n_init, random_state=random_state)
        if imgs[0].ndim == 3:
            print("Fitting model for multichannel images")
            self._fit_multichannel(imgs=imgs, n_init=n_init, random_state=random_state)
        return self

    def fit_transform(self, imgs, n_init=50, random_state=1307):
        if imgs[0].ndim == 2:
            print("Fitting model for greyscale images")
            return self._fit_single_channel(imgs=imgs,
                                     n_init=n_init,
                                     random_state=random_state,
                                     transform=True)
        if imgs[0].ndim == 3:
            print("Fitting model for multichannel images")
            return self._fit_multichannel(imgs=imgs,
                                          n_init=n_init,
                                          random_state=random_state,
                                          transform=True)

    def _transform_single_channel(self, imgs,
                                  blocks=None,
                                  supblocks=None):
        img_tiles = tile_images(imgs, self.tile_size)           
        pixel_mean = (pd.concat([get_block_counts(t) for t in img_tiles]).
                      reindex(columns=self.pixel_types).
                      fillna(0).
                      reset_index(drop=True))
        pixel_mean.columns = ['-'.join(['pixel', str(col)])
                           for col in pixel_mean.columns.values]
        grid_shape = tuple(int(x / y) for x,y in zip(imgs[0].shape, self.tile_size))
        if supblocks is None:
            blockfeats = [get_blockfeats(t) for t in img_tiles]
            blockdf = pd.concat(blockfeats).fillna(0)
            blocks = [get_block_types(bf,
                              km_block=self.km_block,
                              cols=self.pixel_types,
                              grid_shape=grid_shape) for bf in blockfeats]
            supblocks = [get_supblocks(bl) for bl in blocks]
        block_mean = pd.concat([get_block_counts(bl).reindex(columns=range(self.n_block_types+1)) for bl in blocks]).fillna(0).reset_index(drop=True)
        block_mean.columns = ['-'.join(['block', str(col)])
                           for col in block_mean.columns.values]
        supblock_mean = pd.concat([get_block_counts(self.km_supblock.predict(sbf.reindex(columns=range(self.n_block_types + 1)).fillna(0)))
                       for sbf in supblocks]).reset_index(drop=True)
        supblock_mean = supblock_mean.reindex(columns=range(self.n_supblock_types)).fillna(0)
        supblock_mean.columns = ['-'.join(['superblock', str(col+1)])
           for col in supblock_mean.columns.values]
        img_prof = pd.concat([supblock_mean, block_mean,  pixel_mean], axis=1)
        return img_prof

    def _transform_multichannel(self, imgs,
                                img_blocked=None,
                                supblocks=None):
        img_tiles = tile_images(imgs, self.tile_size)
        Xtest = np.concatenate([flatten_tiles(t) for t in img_tiles])
        blockdf = self.pca.transform(Xtest)
        pc_mean = pd.DataFrame(blockdf,
                               columns=['PC'+ str(i+1) for i in range(self.n_components)])
        pc_mean.index = np.repeat(range(len(img_tiles)), img_tiles[0].shape[0])
        pc_mean = pc_mean.groupby(pc_mean.index).agg('mean')
        if supblocks is None:
            grid_shape = tuple(int(x / y) for x,y in zip(imgs[0].shape, self.tile_size))
            img_blocked = self.km_block.predict(blockdf).reshape(-1, *grid_shape)
            supblocks = [get_color_supblocks(img_blocked[i]) for i in range(img_blocked.shape[0])]
        block_mean = pd.concat([get_block_counts(img_blocked[i]).reindex(columns=range(self.n_block_types)) for i in range(img_blocked.shape[0])]).fillna(0).reset_index(drop=True)
        block_mean.columns = ['-'.join(['block', str(col+1)])
                           for col in block_mean.columns.values]
        supblock_mean = pd.concat([get_block_counts(self.km_supblock.predict(sbf.reindex(columns=range(self.n_block_types)).fillna(0)))
                       for sbf in supblocks]).reset_index(drop=True)
        supblock_mean = supblock_mean.reindex(columns=range(self.n_supblock_types)).fillna(0)
        supblock_mean.columns = ['-'.join(['superblock', str(col+1)])
           for col in supblock_mean.columns.values]
        img_prof = pd.concat([supblock_mean, block_mean, pc_mean], axis=1)
        return img_prof


    def transform(self, imgs):
        if imgs[0].ndim == 2:
            return self._transform_single_channel(imgs)
        if imgs[0].ndim == 3:
            return self._transform_multichannel(imgs)
