from scipy.optimize import basinhopping
import multiprocessing
import itertools
import timeit
import matplotlib.gridspec as gridspec
import numpy as np
import matplotlib.pyplot as plt
import astropy.io.fits as pyfits
from astropy.visualization import simple_norm
from webbpsf import wfirst

import psf_mog_fitting as pmf


def gridcreate(name, y, x, ratio, z, **kwargs):
    # Function that creates a blank axis canvas; each figure gets a name (or alternatively a number
    # if none is given), and gridspec creates an N*M grid onto which you can create axes for plots.
    # This returns a gridspec "instance" so you can specific which figure to put the axis on if you
    # have several on the go.
    plt.figure(name, figsize=(z*x, z*ratio*y))
    gs = gridspec.GridSpec(y, x, **kwargs)
    return gs


os = 4
cut = 0.015
max_pix_offset = 10
filters = ['z087', 'y106', 'w149', 'j129', 'h158', 'f184']
psf_names = ['../PSFs/{}.fits'.format(q) for q in filters]
gs = gridcreate('adsq', 4, len(psf_names), 0.8, 5)
# assuming each gaussian component has mux, muy, sigx, sigy, rho, c
psf_comp = np.load('../PSFs/wfirst_psf_comp.npy')
for j in range(0, len(psf_names)):
    print(j)
    f = pyfits.open(psf_names[j])
    # #### WFC3 ####
    # as WFC3-2016-12 suggests that fortran reads these files (x, y, N) we most likely read
    # them as (N, y, x) with the transposition from f- to c-order, thus the psf is (y, x) shape
    # psf_image = f[0].data[4, :, :]
    # #### WFIRST ####
    psf_image = f[0].data

    x, y = np.arange(0, psf_image.shape[1])/os, np.arange(0, psf_image.shape[0])/os
    x_cent, y_cent = (x[-1]+x[0])/2, (y[-1]+y[0])/2
    over_index_middle = 1 / 2
    cut_int = ((x.reshape(1, -1) % 1.0 == over_index_middle) &
               (y.reshape(-1, 1) % 1.0 == over_index_middle))
    # just ignore anything below cut*np.amax(image) to only fit central psf
    total_flux, cut_flux = np.sum(psf_image[cut_int]), \
        np.sum(psf_image[cut_int & (psf_image >= cut * np.amax(psf_image))])
    x -= x_cent
    y -= y_cent
    x_cent, y_cent = 0, 0

    x_w = np.where((x >= -1 * max_pix_offset) & (x <= max_pix_offset))[0]
    y_w = np.where((y >= -1 * max_pix_offset) & (y <= max_pix_offset))[0]

    y_w0, y_w1, x_w0, x_w1 = np.amin(y_w), np.amax(y_w), np.amin(x_w), np.amax(x_w)
    x_, y_ = x[x_w0:x_w1+1], y[y_w0:y_w1+1]

    psf_image_c = np.copy(psf_image[y_w0:y_w1+1, x_w0:x_w1+1])
    x_c, y_c = x[x_w0:x_w1+1], y[y_w0:y_w1+1]

    ax = plt.subplot(gs[0, j])
    ax.set_title(r'Cut flux is {:.3f}\% of total flux'.format(cut_flux/total_flux*100))
    norm = simple_norm(psf_image, 'log', percent=100)
    # with the psf being (y, x) we do not need to transpose it to correct for pcolormesh being
    # flipped, but our x and y need additional tweaking, as these are pixel centers, but
    # pcolormesh wants pixel edges. we thus subtract half a pixel off each value and add a
    # final value to the end
    dx, dy = np.mean(np.diff(x)), np.mean(np.diff(y))
    x_pc, y_pc = np.append(x - dx/2, x[-1] + dx/2), np.append(y - dy/2, y[-1] + dy/2)
    img = ax.pcolormesh(x_pc, y_pc, psf_image, cmap='viridis', norm=norm, edgecolors='face', shading='flat')
    cb = plt.colorbar(img, ax=ax, use_gridspec=True)
    cb.set_label('PSF Response')
    ax.set_xlabel('x / pixel')
    ax.set_ylabel('y / pixel')
    ax.axvline(x_c[0], c='k', ls='-')
    ax.axvline(x_c[-1], c='k', ls='-')
    ax.axhline(y_c[0], c='k', ls='-')
    ax.axhline(y_c[-1], c='k', ls='-')

    p = psf_comp[j].reshape(-1)
    print(pmf.psf_fit_min(p, x, y, psf_image)[0])
    psf_fit = pmf.psf_fit_fun(p, x, y)

    ax = plt.subplot(gs[1, j])
    norm = simple_norm(psf_fit, 'log', percent=100)
    img = ax.pcolormesh(x_pc, y_pc, psf_fit, cmap='viridis', norm=norm, edgecolors='face', shading='flat')
    cb = plt.colorbar(img, ax=ax, use_gridspec=True)
    cb.set_label('PSF Response')
    ax.set_xlabel('x / pixel')
    ax.set_ylabel('y / pixel')
    ax.axvline(x_c[0], c='k', ls='-')
    ax.axvline(x_c[-1], c='k', ls='-')
    ax.axhline(y_c[0], c='k', ls='-')
    ax.axhline(y_c[-1], c='k', ls='-')

    ax = plt.subplot(gs[2, j])
    ratio = np.zeros_like(psf_fit)
    ratio[psf_image != 0] = (psf_fit[psf_image != 0] - psf_image[psf_image != 0]) / \
        psf_image[psf_image != 0]
    ratio_ma = np.ma.array(ratio, mask=(psf_image == 0) & (psf_image > 1e-3))
    norm = simple_norm(ratio[(ratio != 0) & (psf_image > 1e-3)], 'linear', percent=100)
    cmap = plt.get_cmap('viridis')
    cmap.set_bad('w', 0)
    img = ax.pcolormesh(x_pc, y_pc, ratio_ma, cmap=cmap, norm=norm, edgecolors='face', shading='flat')
    cb = plt.colorbar(img, ax=ax, use_gridspec=True)
    cb.set_label('Relative Difference')
    ax.set_xlabel('x / pixel')
    ax.set_ylabel('y / pixel')
    ax.axvline(x_c[0], c='k', ls='-')
    ax.axvline(x_c[-1], c='k', ls='-')
    ax.axhline(y_c[0], c='k', ls='-')
    ax.axhline(y_c[-1], c='k', ls='-')

    p = psf_comp[j].reshape(-1)
    print(psf_comp[j])
    x_, y_ = np.arange(-20, 20.1, 1), np.arange(-20, 20.1, 1)
    print(np.sum(pmf.psf_fit_fun(p, x_, y_)))
    ax = plt.subplot(gs[3, j])
    ratio = (psf_fit - psf_image)
    ratio_ma = np.ma.array(ratio, mask=(psf_image == 0) & (psf_image > 1e-3))
    norm = simple_norm(ratio[(ratio != 0) & (psf_image > 1e-3)], 'linear', percent=100)
    cmap = plt.get_cmap('viridis')
    cmap.set_bad('w', 0)
    img = ax.pcolormesh(x_pc, y_pc, ratio_ma, cmap=cmap, norm=norm, edgecolors='face', shading='flat')
    cb = plt.colorbar(img, ax=ax, use_gridspec=True)
    cb.set_label('Absolute Difference')
    ax.set_xlabel('x / pixel')
    ax.set_ylabel('y / pixel')
    ax.axvline(x_c[0], c='k', ls='-')
    ax.axvline(x_c[-1], c='k', ls='-')
    ax.axhline(y_c[0], c='k', ls='-')
    ax.axhline(y_c[-1], c='k', ls='-')

plt.tight_layout()
plt.savefig('psf_fit/test_psf_mog_test.pdf')