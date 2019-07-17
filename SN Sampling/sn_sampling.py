import os
import sys
import matplotlib.gridspec as gridspec
import numpy as np

import astropy.io.fits as pyfits
import matplotlib.pyplot as plt

from astropy.visualization import simple_norm
from scipy.special import gammaincinv
from astropy.table import Table
import sncosmo
import astropy.units as u
from scipy.ndimage import shift
import glob

import psf_mog_fitting as pmf

import timeit

try:
    dummy = profile
except:
    profile = lambda x: x
np.set_printoptions(edgeitems=10, linewidth=500)
import galsim.wfirst as wfirst

# things to add to detector to create accurate noise model:
# sources, counting as poissonian noise
# dark
# read
# background (zodiacal light)
# thermal background
# reciprocity failure
# non-linearity
# interpixel capacitance
# persistence
# charge diffusion

# nonlinearity_beta - The coefficient of the (counts)^2 term in the detector nonlinearity
# function.  This will not ordinarily be accessed directly by users; instead, it will be accessed
# by the convenience function in this module that defines the nonlinearity function as
# counts_out = counts_in + beta*counts_in^2.

# reciprocity_alpha - The normalization factor that determines the effect of reciprocity failure
# of the detectors for a given exposure time. - use the algorithm galsim uses, in which
# pR/p = ((p/t)/(p'/t'))^(alpha/log(10)). p'/t' is the flux for which the relation holds - with
# wfirst using base_flux = 1.0, p is response in electrons, t is time; pR is the response if the
# relation fails to hold

# thermal background currently 0.023 e/pix/s except F184 which is 0.179 e/pix/s - add catch for
# R062 and default it to z087 otherwise

# ipc_kernel - The 3x3 kernel to be used in simulations of interpixel capacitance (IPC), using
# galsim.wfirst.applyIPC().

# persistence_coefficients - The retention fraction of the previous eight exposures in a simple,
# linear model for persistence.

# charge_diffusion - The per-axis sigma to use for a Gaussian representing charge diffusion for
# WFIRST.  Units: pixels.

# read noise goes as sqrt(sig_floor**2 + 12 * sig_RN**2 * (N-1) / (N+1) / N) where
# N = t_exp / t_read; if N=100 we get read noise of ~8.5 e- for a floor of 5 e- single RN of 20 e-.


def gridcreate(name, y, x, ratio, z, **kwargs):
    # Function that creates a blank axis canvas; each figure gets a name (or alternatively a number
    # if none is given), and gridspec creates an N*M grid onto which you can create axes for plots.
    # This returns a gridspec "instance" so you can specific which figure to put the axis on if you
    # have several on the go.
    plt.figure(name, figsize=(z*x, z*ratio*y))
    gs = gridspec.GridSpec(y, x, **kwargs)
    return gs


def model_number(run_minutes, n_runs):
    # assuming a static time for each run; dominated by fit, not creation currently
    n_filt_choice = 0
    n = 7  # including R, eventually
    for k in np.arange(2, n+1):
        n_filt_choice += np.math.factorial(n) / np.math.factorial(k) / np.math.factorial(n - k)
    # cadence can vary from, say, 5 days to 40 days (5 days being the minimum needed, and 25 days
    # giving 2 data points per lightcurve), so cadence could be varied in 5s initially, and thus
    cadence_interval = 5
    cadences = np.arange(5, 40+1e-10, cadence_interval)
    n_cadence = len(cadences)

    n_tot = n_filt_choice * n_cadence

    time = n_tot * run_minutes * n_runs

    print("{} choices, {} runs, {:.0f}/{:.0f}/{:.0f} approximate minutes/hours/days".format(n_tot, n_runs, time, time/60, time/60/24))


def gaussian_2d(x, x_t, mu, mu_t, sigma):
    det_sig = np.linalg.det(sigma)
    p = np.matmul(x_t - mu_t, np.linalg.inv(sigma))
    # if we don't take the 0, 0 slice we accidentally propagate to shape (len, len, len, len) by
    # having (len, len, 1, 1) shape passed through
    mal_dist_sq = np.matmul(p, (x - mu))[:, :, 0, 0]
    gauss_pdf = np.exp(-0.5 * mal_dist_sq) / (2 * np.pi * np.sqrt(det_sig))
    return gauss_pdf


# flat and dark can be loaded from a fits file or found elsewhere, they are simply input
# files to be multipled/added to the original data.
def add_dark(image, dark_current):
    image += dark_current
    return image


def mult_flat(image, d):
    # choice returns a random choice from np.arange(a) if just given a single integer a
    x_i, y_j = (np.random.choice(d.shape[i]-image.shape[i]) for i in [0, 1])
    image *= d[x_i:x_i + image.shape[0], y_j:y_j + image.shape[1]]
    return image


# read noise is just a constant single read value
def add_read(image, readnoise):
    image += readnoise
    return image


def set_exptime(image, exptime):
    image *= exptime
    return image


def add_background(image, bkg):
    image += bkg
    return image


# if lambda is a numpy array then size is ignored and each value is used creating a new array of
# the original shape. we could instead, for large lambda, generate a gaussian of mean 0 and
# variance lambda; this is the more general formula allowing for low counts, however.
def add_poisson(image):
    return np.random.poisson(lam=image).astype(float)


def mog_galaxy(pixel_scale, filt_zp, psf_c, gal_params):
    mu_0, n_type, e_disk, pa_disk, half_l_r, offset_r, Vgm_unit, mag, offset_ra_pix, \
        offset_dec_pix = gal_params

    cm_exp = np.array([0.00077, 0.01077, 0.07313, 0.37188, 1.39727, 3.56054, 4.74340, 1.78732])
    vm_exp_sqrt = np.array([0.02393, 0.06490, 0.13580, 0.25096, 0.42942, 0.69672, 1.08879,
                            1.67294])
    cm_dev = np.array([0.00139, 0.00941, 0.04441, 0.16162, 0.48121, 1.20357, 2.54182, 4.46441,
                       6.22821, 6.15393])
    vm_dev_sqrt = np.array([0.00087, 0.00296, 0.00792, 0.01902, 0.04289, 0.09351, 0.20168, 0.44126,
                            1.01833, 2.74555])

    # this requires re-normalising as Hogg & Lang (2013) created profiles with unit intensity at
    # their half-light radius, with total flux for the given profile simply being the sum of the
    # MoG coefficients, cm, so we ensure that sum(cm) = 1 for normalisation purposes
    cms = cm_dev / np.sum(cm_dev) if n_type == 4 else cm_exp / np.sum(cm_exp)
    # Vm is always circular so this doesn't need to be a full matrix, but PSF m/V do need to
    vms = np.array(vm_dev_sqrt)**2 if n_type == 4 else np.array(vm_exp_sqrt)**2

    mks = psf_c[:, [0, 1]].reshape(-1, 2, 1)
    pks = psf_c[:, 5]  # what is referred to as 'c' in psf_mog_fitting is p_k in H&L13
    sx, sy, r = psf_c[:, 2], psf_c[:, 3], psf_c[:, 4]
    Vks = np.array([[[sx[q]**2, r[q]*sx[q]*sy[q]], [r[q]*sx[q]*sy[q], sy[q]**2]] for
                    q in range(0, len(sx))])
    # covariance matrix and mean positions given in pixels, but need converting to half-light
    mks *= (pixel_scale / half_l_r)
    Vks *= (pixel_scale / half_l_r)**2

    len_image = np.ceil(2.2*offset_r / pixel_scale).astype(int)
    len_image = len_image + 1 if len_image % 2 == 0 else len_image
    len_image = max(25, len_image)
    image = np.zeros((len_image, len_image+2), float)
    x_cent, y_cent = (image.shape[0]-1)/2, (image.shape[1]-1)/2

    # positons should be in dimensionless but physical coordinates in terms of Re; first the
    # Xg vector needs converting from its given (ra, dec) to pixel coordiantes, to be placed
    # in the xy grid correctly (currently this just defaults to the central pixel, but it may
    # not in the future)
    xg = np.array([[(offset_ra_pix + x_cent) * pixel_scale / half_l_r],
                   [(offset_dec_pix + y_cent) * pixel_scale / half_l_r]])
    x_pos = (np.arange(0, image.shape[0])) * pixel_scale / half_l_r
    y_pos = (np.arange(0, image.shape[1])) * pixel_scale / half_l_r
    x, y = np.meshgrid(x_pos, y_pos, indexing='xy')
    # n-D gaussians have mahalnobis distance (x - mu)^T Sigma^-1 (x - mu) so coords_t and m_t
    # should be *row* vectors, and thus be shape (1, x) while coords and m should be column
    # vectors and shape (x, 1). starting with coords, we need to add the grid of data, so if
    # this array has shape (1, 2, y, x), and if we transpose it it'll have shape (x, y, 2, 1)
    coords = np.transpose(np.array([[x, y]]))
    # the "transpose" of the vector x turns from being a column vector (shape = (2, 1)) to a
    # row vector (shape = (1, 2)), but should still have external shape (x, y), so we start
    # with vector of (2, 1, y, x) and transpose again
    coords_t = np.transpose(np.array([[x], [y]]))
    # total flux in galaxy -- ensure that all units end up in flux as counts/s accordingly
    Sg = 10**(-1/2.5 * (mag - filt_zp))
    for k in range(0, len(mks)):
        pk = pks[k]
        Vk = Vks[k]
        mk = mks[k]
        for m_ in range(0, len(vms)):
            cm = cms[m_]
            vm = vms[m_]
            # Vgm = RVR^T = vm RR^T given that V = vmI
            Vgm = vm * Vgm_unit
            # reshape m and m_t to force propagation of arrays, remembering row vectors are
            # (1, x) and column vectors are (x, 1) in shape
            m = (mk + xg).reshape(1, 1, 2, 1)
            m_t = m.reshape(1, 1, 1, 2)
            V = Vgm + Vk
            g_2d = gaussian_2d(coords, coords_t, m, m_t, V)
            # having converted the covariance matrix to half-light radii, we need to account for a
            # corresponding reverse correction so that the PSF dimensions are correct, which are
            # defined in pure pixel scale
            image += Sg * cm * pk * g_2d / (half_l_r / pixel_scale)**2

    return image


def mog_add_psf(image, psf_params, filt_zp, psf_c):
    offset_ra_pix, offset_dec_pix, mag = psf_params
    x_cent, y_cent = (image.shape[0]-1)/2, (image.shape[1]-1)/2
    # unlike the MoG for the galaxy profile, the PSF can be fit entirely in pure pixel coordinates,
    # with all parameters defined in this coordinate system
    xg = np.array([[offset_ra_pix + x_cent], [offset_dec_pix + y_cent]])
    x_pos, y_pos = np.arange(0, image.shape[0]), np.arange(0, image.shape[1])
    x, y = np.meshgrid(x_pos, y_pos, indexing='xy')
    # n-D gaussians have mahalnobis distance (x - mu)^T Sigma^-1 (x - mu) so coords_t and m_t
    # should be *row* vectors, and thus be shape (1, x) while coords and m should be column
    # vectors and shape (x, 1). starting with coords, we need to add the grid of data, so if
    # this array has shape (1, 2, y, x), and if we transpose it it'll have shape (x, y, 2, 1)
    coords = np.transpose(np.array([[x, y]]))
    # the "transpose" of the vector x turns from being a column vector (shape = (2, 1)) to a
    # row vector (shape = (1, 2)), but should still have external shape (x, y), so we start
    # with vector of (2, 1, y, x) and transpose again
    coords_t = np.transpose(np.array([[x], [y]]))

    mks = psf_c[:, [0, 1]].reshape(-1, 2, 1)
    pks = psf_c[:, 5]  # what is referred to as 'c' in psf_mog_fitting is p_k in H&L13
    sx, sy, r = psf_c[:, 2], psf_c[:, 3], psf_c[:, 4]
    Vks = np.array([[[sx[q]**2, r[q]*sx[q]*sy[q]], [r[q]*sx[q]*sy[q], sy[q]**2]] for
                    q in range(0, len(sx))])

    # total flux in source -- ensure that all units end up in flux as counts/s accordingly
    Sg = 10**(-1/2.5 * (mag - filt_zp))
    for k in range(0, len(mks)):
        pk = pks[k]
        V = Vks[k]
        mk = mks[k]
        # reshape m and m_t to force propagation of arrays, remembering row vectors are
        # (1, x) and column vectors are (x, 1) in shape
        m = (mk + xg).reshape(1, 1, 2, 1)
        m_t = m.reshape(1, 1, 1, 2)
        g_2d = gaussian_2d(coords, coords_t, m, m_t, V)
        image += Sg * pk * g_2d

    return image


def get_sn_model(sn_type, setflag, t0=0.0, z=0.0):
    # salt2 for Ia, s11-* where * is 2004hx for IIL/P, 2005hm for Ib, and 2006fo for Ic
    # draw salt2 x1 and c from salt2_parameters (gaussian, x1: x0=0.4, sigma=0.9, c: x0=-0.04,
    # sigma = 0.1)
    # Hounsell 2017 gives SALT2 models over a wider wavelength range, given as sncosmo source
    # salt2-h17. both salt2 models have phases -20 to +50 days.
    # above non-salt2 models don't give coverage, so trying new ones from the updated builtin
    # source list...

    if sn_type == 'Ia':
        sn_model = sncosmo.Model('salt2-extended-h17')
        if setflag:
            x1, c = np.random.normal(0.4, 0.9), np.random.normal(-0.04, 0.1)
            sn_model.set(t0=t0, z=z, x1=x1, c=c)
    elif sn_type == 'Ib':
        sn_model = sncosmo.Model('snana-2007nc')
        if setflag:
            sn_model.set(t0=t0, z=z)
    elif sn_type == 'Ic':
        sn_model = sncosmo.Model('snana-2006lc')
        if setflag:
            sn_model.set(t0=t0, z=z)
    elif sn_type == 'IIP' or sn_type == 'II':
        sn_model = sncosmo.Model('snana-2007nv')
        if setflag:
            sn_model.set(t0=t0, z=z)
    elif sn_type == 'IIL':
        sn_model = sncosmo.Model('nugent-sn2l')
        if setflag:
            sn_model.set(t0=t0, z=z)
    # TODO: add galaxy dust via smcosmo.F99Dust([r_v])

    return sn_model


def make_images(filters, pixel_scale, sn_type, times, exptime, filt_zp, psf_comp_filename,
                dark_current, readnoise, t0, lambda_eff):
    nfilts = len(filters)
    ntimes = len(times)

    # assuming surface brightnesses vary between roughly mu_e = 18-23 mag/arcsec^2 (mcgaugh
    # 1995, driver 2005, shen 2003 -- assume shen 2003 gives gaussian with mu=20.94, sigma=0.74)

    mu_0 = np.random.normal(20.94, 0.74)
    # elliptical galaxies approximated as de vaucouler (n=4) sersic profiles, spirals as
    # exponentials (n=1). axial ratios vary 0.5-1 for ellipticals and 0.1-1 for spirals
    rand_num = np.random.uniform()
    n_type = 4 if rand_num < 0.5 else 1
    # randomly draw the eccentricity from 0.5/0.1 to 1, depending on sersic index
    e_disk = np.random.uniform(0.5 if n_type == 4 else 0.1, 1.0)
    # position angle can be uniformly drawn [0, 360) as we convert to radians elsewhere
    pa_disk = np.random.uniform(0, 360)
    # half-light radius can be uniformly drawn between two reasonable radii
    lr_low, lr_high = 0.3, 2.5
    half_l_r = np.random.uniform(lr_low, lr_high)
    # L(< R) / Ltot = \gamma(2n, x) / \Gamma(2n); scipy.special.gammainc is lower incomplete over
    # regular gamma function. Thus gammaincinv is the inverse to gammainc, solving
    # L(< r) / Ltot = Y, where Y is a large fraction
    y_frac = 0.75
    x_ = gammaincinv(2*n_type, y_frac)
    # however, x = bn * (R/Re)**(1/n), so we have to solve for R now, approximating bn; in arcsec
    offset_r = (x_ / (2*n_type - 1/3))**n_type * half_l_r
    # redshift randomly drawn between two values uniformly
    z_low, z_high = 0.2, 1.0
    z = np.random.uniform(z_low, z_high)

    psf_comp = np.load(psf_comp_filename)

    # 0.75 mag is really 2.5 * log10(2), for double flux, given area is half-light radius
    mag = mu_0 - 2.5 * np.log10(np.pi * half_l_r**2 * e_disk) - 2.5 * np.log10(2)

    # since everything is defined in units of half-light radius, the "semi-major axis" is always
    # one with the semi-minor axis simply being the eccentricity (b/a, not to be confused with
    # the ellipicity = sqrt((a**2 - b**2)/a**2) = 1 - b/a) of the ellipse
    a, b = 1, e_disk
    t = np.radians(pa_disk)
    Rg = np.array([[-a * np.sin(t), b * np.cos(t)], [a * np.cos(t), b * np.sin(t)]])
    Vgm_unit = np.matmul(Rg, np.transpose(Rg))

    endflag = 0
    while endflag == 0:
        # random offsets for star should be in arcseconds
        rand_ra = -offset_r + np.random.random_sample() * 2 * offset_r
        rand_dec = -offset_r + np.random.random_sample() * 2 * offset_r
        # the full equation for a shifted, rotated ellipse, with semi-major axis
        # originally aligned with the y-axis, is given by:
        # ((x-p)cos(t)-(y-q)sin(t))**2/b**2 + ((x-p)sin(t) + (y-q)cos(t))**2/a**2 = 1
        p = 0
        q = 0
        x = rand_ra
        y = rand_dec
        t = np.radians(pa_disk)
        a = offset_r
        b = e_disk * offset_r
        if (((((x - p) * np.cos(t) - (y - q) * np.sin(t)) / b)**2 +
             (((x - p) * np.sin(t) + (y - q) * np.cos(t)) / a)**2 <= 1) and
            ((((x - p) * np.cos(t) - (y - q) * np.sin(t)) / b)**2 +
             (((x - p) * np.sin(t) + (y - q) * np.cos(t)) / a)**2 > 0.05)):
            endflag = 1

    sn_model = get_sn_model(sn_type, 1, t0=t0, z=z)
    # pretending that F125W on WFC3/IR is 2MASS J, we set the absolute magnitude of a
    # type Ia supernova to J = -19.0 (meikle 2000). Phillips (1993) also says that ~M_I = -19 --
    # currently just setting absolute magnitudes to -19, but could change if needed
    sn_model.set_source_peakabsmag(-19.0, 'f125w', 'ab')

    images_with_sn = []
    images_without_sn = []
    diff_images = []

    # things that are needed to create the astropy.table.Table for use in fit_lc:
    # time, band (name, see registered bandpasses), flux, fluxerr [both just derived from an
    # image somehow], zp, zpsys [zeropoint and name of system]

    time_array = []
    band_array = []
    flux_array = []
    fluxerr_array = []
    zp_array = []
    zpsys_array = []

    # TODO: see if we can replace this with the galsim.wfirst version
    # given some zodiacal light flux, in ergcm^-2s^-1A^-1arcsec^-2, flip given the ST ZP,
    # then convert back to flux;
    # see http://www.stsci.edu/hst/wfc3/documents/handbooks/currentIHB/c09_exposuretime08.html
    zod_flux_st = 2e-18  # erg/cm^2/s/A/arcsec^2; Fl = c/l^2 Fv
    # erg/cm^2/s/Hz/arcsec^2; l_eff in um; final conversion of 1e-10 makes it 1/(s^-1 ang^-1)
    zod_flux = zod_flux_st / (3e8 / (lambda_eff*1e-6)**2 * 1e-10)
    zod_flux *= pixel_scale**2  # erg/cm^2/s/Hz[/pixel]
    zod_mag = -2.5 * np.log10(zod_flux) - 48.6  # AB mag system
    zod_count = 10**(-1/2.5 * (zod_mag - filt_zp[0]))
    # correct the zodiacal light counts for the stray light fraction of the telescope
    zod_count *= (1.0 + wfirst.stray_light_fraction)
    gal_params = [mu_0, n_type, e_disk, pa_disk, half_l_r, offset_r, Vgm_unit, mag]
    # TODO: check if simple half-pixel dither is right and update if not
    second_gal_offets = np.empty((nfilts, 2), float)
    for j in range(0, nfilts):
        # define a random pixel offset ra/dec
        offset_ra, offset_dec = np.random.uniform(0.01, 0.99), np.random.uniform(0.01, 0.99)
        sign = -1 if np.random.uniform(0, 1) < 0.5 else 1
        # non-reference image should be offset by half a pixel, wrapped around [0, 1]
        second_gal_offets[j, 0] = (offset_ra + sign * 0.5 + 1) % 1
        second_gal_offets[j, 1] = (offset_dec + sign * 0.5 + 1) % 1
        image = mog_galaxy(pixel_scale, filt_zp[j], psf_comp[j], gal_params +
                           [offset_ra, offset_dec])
        q = np.where(image < 0)
        image[q] = 1e-8
        image = add_background(image, zod_count[j])
        image = add_dark(image, dark_current)
        image = set_exptime(image, exptime)
        image = add_poisson(image)
        image = add_read(image, readnoise)

        # second_gal_offset is the pixel offset, relative to the central pixel, of the observation,
        # onto which we should shift the 'reference' frame. we therefore are asking given
        # reference pixel x', what dx do we add such that x' + dx = x? Thus, dx = x - x', or
        # observation pixel minus reference pixel; assume scipy.ndimage.shift correctly spline
        # interpolates the shift as required.
        dx_, dy_ = second_gal_offets[j, 0] - offset_ra, second_gal_offets[j, 1] - offset_dec
        image = shift(image, [dx_, dy_], mode='nearest')
        images_without_sn.append(image)

    true_flux = []
    for k in range(0, ntimes):
        images = []
        images_diff = []
        for j in range(0, nfilts):
            image_shifted = images_without_sn[j]
            # TODO: add exposure and readout time so that exposures are staggered in time
            time = times[k] + t0

            # get the apparent magnitude of the supernova at a given time; first get the
            # appropriate filter for the observation
            bandpass = sncosmo.get_bandpass(filters[j])
            # time should be in days
            m_ia = sn_model.bandmag(bandpass, magsys='ab', time=time)
            if np.isnan(m_ia):
                m_ia = -2.5 * np.log10(0.01) + filt_zp[j]

            # if cosmicrays are needed then figure out what stips does for that...
            offset_ra, offset_dec = second_gal_offets[j, :]
            image = mog_galaxy(pixel_scale, filt_zp[j], psf_comp[j],
                               gal_params + [offset_ra, offset_dec])
            image = mog_add_psf(image, [rand_ra / pixel_scale, rand_dec / pixel_scale, m_ia],
                                filt_zp[j], psf_comp[j])
            q = np.where(image < 0)
            image[q] = 1e-8
            image = add_background(image, zod_count[j])
            image = add_dark(image, dark_current)
            image = set_exptime(image, exptime)
            image = add_poisson(image)
            image = add_read(image, readnoise)

            images.append(image)
            image_diff = image - image_shifted
            images_diff.append(image_diff)

            time_array.append(time)
            band_array.append(filters[j])

            x_cent, y_cent = (image.shape[0]-1)/2, (image.shape[1]-1)/2
            xind, yind = np.floor(rand_ra / pixel_scale + x_cent).astype(int), np.floor(rand_dec / pixel_scale + y_cent).astype(int)

            N = 5
            delta = np.arange(-N, N+1e-10, 1)
            p = psf_comp[j].reshape(-1)
            # rand_* is (fractional) pixel offset from centre, so we just modulo 1 to get single
            # pixel fraction
            dx, dy = (rand_ra/pixel_scale) % 1, (rand_dec/pixel_scale) % 1
            psf_box_sum = np.sum(pmf.psf_fit_fun(p, delta+dx, delta+dy))
            # current naive sum the entire (box) 'aperture' flux of the Sn, correcting for
            # exposure time in both counts and uncertainty; also have to correct for the lost flux
            # outside of the box
            xind0, xind1 = max(0, xind-N), min(image_diff.shape[0], xind+N+1)
            yind0, yind1 = max(0, yind-N), min(image_diff.shape[1], yind+N+1)
            diff_sum = np.sum(image_diff[xind0:xind1, yind0:yind1]) / exptime / psf_box_sum
            diff_sum_err = np.sqrt(np.sum(image[xind0:xind1, yind0:yind1] +
                                          image_shifted[xind0:xind1, yind0:yind1])) / \
                exptime/psf_box_sum
            flux_array.append(diff_sum)
            fluxerr_array.append(diff_sum_err)
            zp_array.append(filt_zp[j])  # filter-specific zeropoint
            zpsys_array.append('ab')

            true_flux.append(10**(-1/2.5 * (m_ia - filt_zp[j])))
        images_with_sn.append(images)
        diff_images.append(images_diff)

    lc_data = [np.array(time_array), np.array(band_array), np.array(flux_array),
               np.array(fluxerr_array), np.array(zp_array), np.array(zpsys_array)]
    true_flux = np.array(true_flux)

    param_names = ['z', 't0']
    if sn_type == 'Ia':
        param_names += ['x0', 'x1', 'c']
    else:
        param_names += ['amplitude']
    sn_params = [sn_model[q] for q in param_names]

    return images_with_sn, images_without_sn, diff_images, lc_data, sn_params, true_flux


def make_fluxes(filters, sn_type, times, filt_zp, t0):
    nfilts = len(filters)
    ntimes = len(times)

    # redshift randomly drawn between two values uniformly
    z_low, z_high = 0.2, 1.0
    z = np.random.uniform(z_low, z_high)

    sn_model = get_sn_model(sn_type, 1, t0=t0, z=z)
    # pretending that F125W on WFC3/IR is 2MASS J, we set the absolute magnitude of a
    # type Ia supernova to J = -19.0 (meikle 2000). Phillips (1993) also says that ~M_I = -19 --
    # currently just setting absolute magnitudes to -19, but could change if needed
    sn_model.set_source_peakabsmag(-19.0, 'f125w', 'ab')

    # things that are needed to create the astropy.table.Table for use in fit_lc:
    # time, band (name, see registered bandpasses), flux, fluxerr [both just derived from an
    # image somehow], zp, zpsys [zeropoint and name of system]

    time_array = []
    band_array = []
    flux_array = []
    fluxerr_array = []
    zp_array = []
    zpsys_array = []

    true_flux = []
    for k in range(0, ntimes):
        for j in range(0, nfilts):
            # TODO: add exposure and readout time so that exposures are staggered in time
            time = times[k] + t0

            # get the apparent magnitude of the supernova at a given time; first get the
            # appropriate filter for the observation
            bandpass = sncosmo.get_bandpass(filters[j])
            # time should be in days
            m_ia = sn_model.bandmag(bandpass, magsys='ab', time=time)
            if np.isnan(m_ia):
                m_ia = -2.5 * np.log10(0.01) + filt_zp[j]

            t_f = 10**(-1/2.5 * (m_ia - filt_zp[j]))

            # noise floor of, say, 0.5% photometry in quadrature with shot noise
            flux_err = np.sqrt(np.sqrt(t_f)**2 + (0.005 * t_f)**2)
            flux = np.random.normal(loc=t_f, scale=flux_err)
            time_array.append(time)
            band_array.append(filters[j])
            flux_array.append(flux)
            fluxerr_array.append(flux_err)
            zp_array.append(filt_zp[j])  # filter-specific zeropoint
            zpsys_array.append('ab')

            true_flux.append(t_f)

    lc_data = [np.array(time_array), np.array(band_array), np.array(flux_array),
               np.array(fluxerr_array), np.array(zp_array), np.array(zpsys_array)]
    true_flux = np.array(true_flux)

    param_names = ['z', 't0']
    if sn_type == 'Ia':
        param_names += ['x0', 'x1', 'c']
    else:
        param_names += ['amplitude']
    sn_params = [sn_model[q] for q in param_names]

    return lc_data, sn_params, true_flux


@profile
def fit_lc(lc_data, sn_types, directory, filters, counter, figtext, ncol, minsnr, sn_priors,
           filt_zp, make_fit_figs, multi_z_fit, type_ind):
    x2s = np.empty(len(sn_types), float)
    bestfit_models = []
    bestfit_results = []
    largest_z = 2.5
    dz = 0.01
    min_counts = 0.0001

    for i, sn_type in enumerate(sn_types):
        params = ['t0', 'z']
        if sn_type == 'Ia':
            params += ['x0', 'x1', 'c']
        else:
            params += ['amplitude']
        sn_model = get_sn_model(sn_type, 0)

        # place upper limits on the redshift probeable, by finding the z at which each filter drops
        # out of being in overlap with the model
        z_upper_band = np.empty(len(filters), float)
        for p in range(0, len(filters)):
            z = 0
            while sn_model.bandoverlap(filters[p], z=z):
                z += dz
                if z > largest_z:
                    break  # otherwise this will just keep going forever for very red filters
            z_upper_band[p] = min(largest_z, z - dz)
        z_upper_count = np.empty(len(filters), float)
        z_lower_count = np.empty(len(filters), float)
        # the lower limits on z -- for this model -- are, assuming a minsnr detection in that
        # filter, a model flux in the given system of, say, 0.0001 counts/s; a very low goal, but
        # one that avoids bluer SNe being selected when they would drop out of the detection. Also
        # avoids models from failing to calculate an amplitude... Similarly, we can calculate the
        # maximum redshift for a blue filter to have a "detection". If there is no detection in
        # this filter, we set the redshift range to its maximum to remove the filter from
        # consideration.
        for p in range(0, len(filters)):
            z_array = np.arange(0, z_upper_band[p]+1e-10, dz)
            snr_filt = lc_data['flux'].data[p] / lc_data['fluxerr'].data[p]
            if snr_filt < minsnr:
                z_upper_count[p] = z_array[-1]
                z_lower_count[p] = z_array[0]
                continue
            countrate = np.empty_like(z_array)
            for q, z_init in enumerate(z_array):
                sn_model.set(z=z_init)
                countrate[q] = sn_model.bandflux(filters[p], time=0, zp=filt_zp[p], zpsys='ab')
            z_upper_count[p] = z_array[np.where(countrate > min_counts)[0][-1]]
            z_lower_count[p] = z_array[np.where(countrate > min_counts)[0][0]]
        # set the bounds on z to be at most the smallest of those available by the given filters in
        # the set being fit here
        z_min = np.amax(z_lower_count)
        z_max = min(np.amin(z_upper_band), np.amin(z_upper_count))
        bounds = {}
        # x1 and c bounded by 3.5-sigma regions (x1: mu=0.4, sigma=0.9, c: mu=-0.04, sigma = 0.1)
        if sn_type == 'Ia':
            bounds.update({'x1': (-2.75, 3.55), 'c': (-0.39, 0.31)})

        if multi_z_fit:
            result = None
            fitted_model = None
            for z_init in np.linspace(z_min, z_max, 10):
                sn_model.set(z=z_init)
                result_temp, fitted_model_temp = sncosmo.fit_lc(lc_data, sn_model, params,
                                                                bounds=bounds, minsnr=minsnr,
                                                                guess_z=False)
                if result is None or result_temp.chisq < result.chisq:
                    result = result_temp
                    fitted_model = fitted_model_temp
        else:
            fitted_model = sn_model

        # after a round of minimising the lightcurve at fixed redshifts, add redshift to allow a
        # final fit of the model to the data
        bounds.update({'z': (z_min, z_max)})
        guess_z = True if fitted_model is sn_model else False
        result, fitted_model = sncosmo.fit_lc(lc_data, fitted_model, params, bounds=bounds,
                                              minsnr=minsnr, guess_z=guess_z)
        bestfit_models.append(fitted_model)
        bestfit_results.append(result)
        try:
            x2s[i] = result.chisq
        except AttributeError:
            x2s[i] = sncosmo.chisq(lc_data, fitted_model)

    # TODO: add a fire extinguisher null hypothesis probability properly
    probs = np.append(sn_priors*np.exp(-0.5 * x2s), 1e-5)
    probs /= np.sum(probs)
    best_ind = np.argmax(probs[:-1])
    best_r = bestfit_results[best_ind]
    best_m = bestfit_models[best_ind]
    best_x2 = x2s[best_ind]
    if np.argmax(probs) == len(probs) - 1:
        fit_type = 'None'
    else:
        fit_type = sn_types[best_ind]
    like_fit_type = 'None' if np.argmin(np.append(x2s, 10)) == len(x2s) else sn_types[np.argmin(np.append(x2s, 10))]
    print(x2s / (len(lc_data['flux']) - len(sn_model.param_names)), probs, sn_types[type_ind], fit_type, like_fit_type)

    if make_fit_figs:
        figtext = [figtext[0], figtext[1] + '\n' + r'$\chi^2_{{\nu={}}}$ = {:.3f}'.format(
                   best_r.ndof, best_x2/best_r.ndof)]
        errors = best_r.errors
        model_params = best_m.parameters
        if sn_types[best_ind] == 'Ia':
            z_format = sncosmo.utils.format_value(model_params[0], errors.get('z'), latex=True)
            t0_format = sncosmo.utils.format_value(model_params[1], errors.get('t0'), latex=True)
            x0_format = sncosmo.utils.format_value(model_params[2], errors.get('x0'), latex=True)
            x1_format = sncosmo.utils.format_value(model_params[3], errors.get('x1'), latex=True)
            c_format = sncosmo.utils.format_value(model_params[4], errors.get('c'), latex=True)
            figtext.append('Type {}: $z = {}$\n$t_0 = {}$\n$x_0 = {}$'.format(sn_types[best_ind],
                           z_format, t0_format, x0_format))
            if probs[0] > 0:
                p_sig = int(np.floor(np.log10(abs(probs[0]))))
            else:
                p_sig = 0
            if p_sig > 3:
                figtext.append('$x_1 = {}$\n$c = {}$\n$P(Ia|D) = {:.3f} \\times 10^{}$'.format(
                               x1_format, c_format, probs[0]/10**p_sig, p_sig))
            else:
                figtext.append('$x_1 = {}$\n$c = {}$\n$P(Ia|D) = {:.3f}$'.format(x1_format,
                               c_format, probs[0]))
        else:
            z_format = sncosmo.utils.format_value(model_params[0], errors.get('z'), latex=True)
            t0_format = sncosmo.utils.format_value(model_params[1], errors.get('t0'), latex=True)
            A_format = sncosmo.utils.format_value(model_params[2], errors.get('amplitude'),
                                                  latex=True)
            figtext.append('Type {}: $z = {}$\n$t_0 = {}$'.format(sn_types[best_ind],
                           z_format, t0_format))
            if probs[0] > 0:
                p_sig = int(np.floor(np.log10(abs(probs[0]))))
            else:
                p_sig = 0
            if p_sig > 3:
                figtext.append('$A = {}$\n$P(Ia|D) = {:.3f} \\times 10^{{{}}}$'.format(
                    A_format, probs[0]/10**p_sig, p_sig))
            else:
                figtext.append('$A = {}$\n$P(Ia|D) = {:.3f}$'.format(A_format, probs[0]))

        ypad = 4 if sn_types[best_ind] == 'Ia' else 2
        fig = sncosmo.plot_lc(lc_data, model=bestfit_models, xfigsize=5*ncol, tighten_ylim=False,
                              ncol=ncol, figtext=figtext, figtextsize=ypad, model_label=sn_types)
        fig.tight_layout(rect=[0, 0.03, 1, 0.935])
        fig.savefig('{}/fit_{}.pdf'.format(directory, counter))

    return probs[0], fit_type


@profile
def run_filt_cadence_combo(sn_types, filters, pixel_scale, times, exptime, filt_zp,
                           psf_comp_filename, dark_current, readnoise, t0, lambda_eff,
                           make_sky_figs, make_fit_figs, make_flux_figs, image_flag, ngals,
                           multi_z_fit):
    probs, true_types, fit_types = [], [], []
    i = 0
    while i < ngals:
        type_ind = np.random.choice(len(sn_types))
        if image_flag:
            images_with_sn, images_without_sn, diff_images, lc_data, sn_params, true_flux = \
                make_images(filters, pixel_scale, sn_types[type_ind], times, exptime, filt_zp,
                            psf_comp_filename, dark_current, readnoise, t0, lambda_eff)
        else:
            lc_data, sn_params, true_flux = make_fluxes(filters, sn_types[type_ind], times,
                                                        filt_zp, t0)

        if make_sky_figs and image_flag:
            make_figures(images_with_sn, images_without_sn, diff_images, filters, times, i+1,
                         exptime)

        lc_data_table = Table(data=lc_data,
                              names=['time', 'band', 'flux', 'fluxerr', 'zp', 'zpsys'])
        if not np.amax(lc_data_table['flux'].data / lc_data_table['fluxerr'].data) >= minsnr:
            continue

        figtext = []
        if sn_types[type_ind] == 'Ia':
            z_, t_, x0_, x1_, c_ = sn_params
            figtext.append('Type {}: $z = {:.3f}$\n$t_0 = {:.1f}$\n'
                           '$x_0 = {:.5f}$'.format(sn_types[type_ind], z_, t_, x0_))
            figtext.append('$x_1 = {:.5f}$\n$c = {:.5f}$'.format(x1_, c_))
        else:
            z_ = sn_params[0]
            t_ = sn_params[1]
            A_ = sn_params[2]
            A_sig = int(np.floor(np.log10(abs(A_))))
            figtext.append('Type {}: $z = {:.3f}$\n$t_0 = {:.1f}$'.format(
                           sn_types[type_ind], z_, t_))
            figtext.append('$A = {:.3f} \\times 10^{{{}}}$'.format(A_/10**A_sig, A_sig))

        prob, fit_type = fit_lc(lc_data_table, sn_types, directory, filters, i+1, figtext, ncol,
                                minsnr, sn_priors, filt_zp, make_fit_figs, multi_z_fit, type_ind)

        if make_flux_figs:
            gs = gridcreate('09', 1, 1, 0.8, 5)
            ax = plt.subplot(gs[0])
            for c, filter_ in zip(colours, filters):
                q = lc_data_table['band'] == filter_
                ax.errorbar(lc_data_table['time'][q], (lc_data_table['flux'][q] - true_flux[q]) /
                            true_flux[q], yerr=lc_data_table['fluxerr'][q]/true_flux[q],
                            fmt='{}.'.format(c), label=filter_)
            ax.legend(shadow=False, framealpha=0)
            ax.axhline(0, c='k', ls='--')
            ax.set_xlabel('Time')
            ax.set_ylabel('Flux difference (fit - true)/true')
            plt.tight_layout()
            plt.savefig('{}/flux_ratio_{}.pdf'.format(directory, i+1))

        # if the original SN is a Ia, then prob will be a "goodness of Ia-ness", but if the SN is
        # a CC (Ib/Ic/II) then the probability will be a "badness of CC-ness"; if we're fitting a
        # true Ia then we want the probability to be high, otherwise it should be low, but in
        # all cases the probability is P(Ia|D) = P(Ia) * p(D|Ia) / sum_j P(j) p(D|j)
        probs.append(prob)
        true_types.append(sn_types[type_ind])
        fit_types.append(fit_type)
        i += 1
    # as well as the overall goodness of Ia and badness of CC arrays, we should also pass back the
    # false positive and false negative rates. FPs are non-Ias fit as Ias, and false negatives are
    # Ias fit as non-Ias
    probs, true_types, fit_types = np.array(probs), np.array(true_types), np.array(fit_types)
    q = true_types == 'Ia'
    goodness_of_ia = np.percentile(probs[q], [16, 50, 84])
    badness_of_cc = np.percentile(probs[np.logical_not(q)], [16, 50, 84])
    fp_rate = np.sum((true_types != 'Ia') & (fit_types == 'Ia')) / ngals
    fn_rate = np.sum((true_types == 'Ia') & (fit_types != 'Ia')) / ngals
    return goodness_of_ia, badness_of_cc, fp_rate, fn_rate


def make_figures(images_with_sn, images_without_sn, diff_images, filters, times, i, exptime):
    n = np.random.choice(len(times))
    t = times[n]
    iws = images_with_sn[n]
    ds = diff_images[n]
    gs = gridcreate('1', 3, len(filters), 0.8, 5)
    for j, (iw, iwo, d, f) in enumerate(zip(iws, images_without_sn, ds, filters)):
        ax = plt.subplot(gs[0, j])
        norm = simple_norm(iw / exptime, 'linear', percent=100)
        img = ax.imshow(iw / exptime, cmap='viridis', norm=norm, origin='lower')
        cb = plt.colorbar(img, ax=ax, use_gridspec=True)
        cb.set_label(r'Flux / e$^-\,\mathrm{s}^{-1}$')
        ax.set_xlabel('x / pixel')
        ax.set_ylabel('y / pixel, {}, t = {:.0f}'.format(f, t))
        ax = plt.subplot(gs[1, j])
        norm = simple_norm(iwo / exptime, 'linear', percent=100)
        img = ax.imshow(iwo / exptime, cmap='viridis', norm=norm, origin='lower')
        cb = plt.colorbar(img, ax=ax, use_gridspec=True)
        cb.set_label(r'Flux / e$^-\,\mathrm{s}^{-1}$')
        ax.set_xlabel('x / pixel')
        ax.set_ylabel('y / pixel')
        ax = plt.subplot(gs[2, j])
        norm = simple_norm(d / exptime, 'linear', percent=100)
        img = ax.imshow(d / exptime, cmap='viridis', norm=norm, origin='lower')
        cb = plt.colorbar(img, ax=ax, use_gridspec=True)
        cb.set_label(r'Flux / e$^-\,\mathrm{s}^{-1}$')
        ax.set_xlabel('x / pixel')
        ax.set_ylabel('y / pixel')
    plt.tight_layout()
    plt.savefig('out_gals/images_{}.pdf'.format(i))
    plt.close()


if __name__ == '__main__':
    ngals = 10
    # run_mins = 20/60
    # model_number(run_mins, ngals)

    # sys.exit()

    directory = 'out_gals'

    filters_master = np.array(['z087', 'y106', 'w149', 'j129', 'h158', 'f184'])  # 'r062'
    # 1 count/s for infinite aperture, hounsell17, AB magnitudes
    # get r062 ZP if added; microsit uses 26.39 for both z087 and r602; microsit disagrees on h158
    # by ~0.03 mags - full microsit ZPs are
    # [26.39 r062] 26.39 26.42 [27.50 w149 mask, 27.61 no mask w149] [25.59 k208] 26.30 25.96
    filt_zp_master = np.array([26.39, 26.41, 27.50, 26.35, 26.41, 25.96])
    lambda_eff_master = np.array([0.601, 0.862, 1.045, 1.251, 1.274, 1.555, 1.830])
    for j in range(0, len(filters_master)):
        f = pyfits.open('../../webbpsf-data/WFI/filters/{}_throughput.fits'.format(
                        filters_master[j].upper()))
        data = f[1].data
        dispersion = np.array([d[0] * 1e-4 for d in data])
        transmission = np.array([d[1] * 0.95 for d in data])
        # both F184 and W149 extend 0.004 microns into 2 microns, beyond the wavelength range of
        # the less extended models, 19990A, or 1.999 microns. Thus we slightly chop the ends off
        # these filters, and set the final 'zero' to 1.998 microns:
        if filters_master[j] == 'f184' or filters_master[j] == 'w149':
            ind_ = np.where(dispersion < 1.999)[0][-1]
            dispersion[ind_+1] = 1.9998
            dispersion[ind_+2] = 1.99985
        q_ = np.argmax(transmission)
        if transmission[q_] == transmission[q_+1]:
            q_ += 1
        imin = np.where(transmission[:q_] == 0)[0][-1]
        imax = np.where(transmission[q_:] == 0)[0][0] + q_ + 1
        bandpass = sncosmo.Bandpass(dispersion[imin:imax], transmission[imin:imax],
                                    wave_unit=u.micron, name=filters_master[j])
        sncosmo.register(bandpass)
    # default exptime assumes ~4 filter pointings in an hour, which is a zeroth order guess at the
    # observing plan for WFIRST at present
    exptime = 1000  # seconds
    sn_types = ['Ia', 'Ib', 'Ic', 'II']

    t_low, t_high, t_interval = -5, 35, 20
    times = np.arange(t_low, t_high+1e-10, t_interval)

    psf_comp_filename = '../PSFs/wfirst_psf_comp.npy'
    psf_names = ['../PSFs/{}.fits'.format(q) for q in filters_master]
    oversampling, N_comp, max_pix_offsets, cuts = 4, 20, [9, 9, 9, 10, 11, 11], [0.0009, 0.0009, 0.0009, 0.0008, 0.0008, 0.0007]

    # pmf.psf_mog_fitting(psf_names, oversampling, psf_comp_filename, N_comp,
    #                     'wfirst', max_pix_offsets, cuts)
    # sys.exit()

    # dark current and read noise from the GalSim instrument; read noise is in pure e-, but
    # the current is e-/pixel/s, so requires correcting by exposure time
    readnoise = wfirst.read_noise
    dark_current = wfirst.dark_current
    pixel_scale = wfirst.pixel_scale  # arcsecond/pixel

    t0 = 50000
    minsnr = 5

    ncol = min(3, len(filters_master))

    # priors on supernovae types: very roughly, these are the relative fractions of each type in
    # the universe, to set the relative likelihoods of the observations with no information; these
    # should follow sn_types as [Ia, Ib, Ic, II]. Boissier & prantzos 2009 quote, roughly and
    # drawn by eye: Ibc/II ~ 0.3, Ic/Ib ~ 1.25, Ia/CC ~ 0.25. Hakobyan 2014, table 8, give:
    NiaNcc, NibcNii, NicNib = 0.44, 0.36, 2.12
    # given a/b=x we get a = x/(1+x) and b = 1/(1+x) = 1 - x/(1+x), so we can convert these to
    # relative fractions:
    fia, fcc = NiaNcc / (1 + NiaNcc), 1 - NiaNcc / (1 + NiaNcc)
    fibc, fii = fcc * NibcNii / (1 + NibcNii), fcc * (1 - NibcNii / (1 + NibcNii))
    fib, fic = fibc * (1 - NicNib / (1 + NicNib)), fibc * NicNib / (1 + NicNib)
    sn_priors = np.array([fia, fib, fic, fii])

    colours_master = np.array(['k', 'r', 'b', 'g', 'c', 'm', 'orange'])

    sub_inds = [0, 3, 4]
    filters = filters_master[sub_inds]
    filt_zp = filt_zp_master[sub_inds]
    colours = colours_master[sub_inds]
    lambda_eff = lambda_eff_master[sub_inds]

    if len(filters) * len(times) <= 5:
        print("Filter/cadence combination does not produce sufficient data points for fitting, please increase one or both parameters.")

    if len(glob.glob('{}/fit_*.pdf'.format(directory))) > 0:
        os.system('rm {}/fit_*.pdf'.format(directory))
    if len(glob.glob('{}/flux_*.pdf'.format(directory))) > 0:
        os.system('rm {}/flux_*.pdf'.format(directory))
    if len(glob.glob('{}/images_*.pdf'.format(directory))) > 0:
        os.system('rm {}/images_*.pdf'.format(directory))

    make_sky_figs, make_fit_figs, make_flux_figs, image_flag = False, False, False, False
    multi_z_fit = False

    start = timeit.default_timer()
    g_ia, b_cc, fp, fn = run_filt_cadence_combo(sn_types, filters, pixel_scale, times, exptime,
                                                filt_zp, psf_comp_filename, dark_current,
                                                readnoise, t0, lambda_eff, make_sky_figs,
                                                make_fit_figs, make_flux_figs, image_flag, ngals,
                                                multi_z_fit)
    time = '{:.0f}s'.format(timeit.default_timer()-start)
    print(g_ia, b_cc, fp, fn, time)
