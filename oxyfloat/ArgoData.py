import os
import re
import logging
import urllib2
import requests
import pandas as pd
import pydap.client
import pydap.exceptions
import xray

from bs4 import BeautifulSoup
from contextlib import closing
from requests.exceptions import ConnectionError
from shutil import move

# Support Python 2.7 and 3.x
try:
    from io import StringIO
except ImportError:
    from cStringIO import StringIO

from exceptions import RequiredVariableNotPresent

class ArgoData(object):
    '''Collection of methods for working with Argo profiling float data.
    '''

    # Jupyter Notebook defines a root logger, use that if it exists
    if logging.getLogger().handlers:
        _notebook_handler = logging.getLogger().handlers[0]
        logger = logging.getLogger()
    else:
        logger = logging.getLogger(__name__)
        _handler = logging.StreamHandler()
        _formatter = logging.Formatter('%(levelname)s %(asctime)s %(filename)s '
                                      '%(funcName)s():%(lineno)d %(message)s')
        _handler.setFormatter(_formatter)
        logger.addHandler(_handler)

    _log_levels = (logging.ERROR, logging.WARN, logging.INFO, logging.DEBUG)

    # Literals for groups stored in local HDF file cache
    _STATUS = 'status'
    _GLOBAL_META = 'global_meta'
    _coordinates = {'PRES_ADJUSTED', 'LATITUDE', 'LONGITUDE', 'JULD'}

    # Names and search patterns for cache file naming/parsing
    # Make private and ignore pylint's complaints
    # No other names in this class can end in 'RE'
    _fixed_cache_base = 'oxyfloat_fixed_cache'
    _ageRE = 'age([0-9]+)'
    _profilesRE = 'profiles([0-9]+)'
    _pressureRE = 'pressure([0-9]+)'

    def __init__(self, verbosity=0, cache_file=None, oxygen_required=True,
            status_url='http://argo.jcommops.org/FTPRoot/Argo/Status/argo_all.txt',
            global_url='ftp://ftp.ifremer.fr/ifremer/argo/ar_index_global_meta.txt',
            thredds_url='http://tds0.ifremer.fr/thredds/catalog/CORIOLIS-ARGO-GDAC-OBS',
            variables=('TEMP_ADJUSTED', 'PSAL_ADJUSTED', 'DOXY_ADJUSTED', 
                       'PRES_ADJUSTED', 'LATITUDE', 'LONGITUDE', 'JULD')):

        '''Initialize ArgoData object.
        
        Args:
            verbosity (int): range(4), default=0
            cache_file (str): Defaults to oxyfloat_cache.hdf next to module
            oxygen_required (boolean): Save profile only if oxygen data exist
            status_url (str): Source URL for Argo status data, defaults to
                http://argo.jcommops.org/FTPRoot/Argo/Status/argo_all.txt
            global_url (str): Source URL for DAC locations, defaults to
                ftp://ftp.ifremer.fr/ifremer/argo/ar_index_global_meta.txt
            thredds_url (str): Base URL for THREDDS Data Server, defaults to
                http://tds0.ifremer.fr/thredds/catalog/CORIOLIS-ARGO-GDAC-OBS
            variables (list): Variables to extract from NetCDF files

        cache_file:

            There are 3 kinds of cache files:

            1. The default file named oxyfloat_cache.hdf that is automatically
               placed in the oxyfloat module directory. It will cache whatever
               data is requested via call to get_float_dataframe().
            2. Specially named cache_files produced by the load_cache.py program
               in the scripts directory. These files are built with constraints
               and are fixed. Once built they can be used in a read-only fashion
               to work on only the data they contain. Calls to get_float_dataframe()
               will not add more data to these "fixed" cache files.
            3. Custom cache file names. These operate just like the default cache
               file, but can be named whatever the user wants. 

        '''
        self.status_url = status_url
        self.global_url = global_url
        self.thredds_url = thredds_url
        self.variables = set(variables)

        self.logger.setLevel(self._log_levels[verbosity])
        self._oxygen_required = oxygen_required

        if cache_file:
            self.cache_file_parms = self._get_cache_file_parms(cache_file)
            self.cache_file = cache_file
        else:
            # Write to same directory where this module is installed
            self.cache_file = os.path.abspath(os.path.join(
                              os.path.dirname(__file__), 'oxyfloat_cache.hdf'))

    def _repack_hdf(self):
        '''Execute the ptrepack command on the cache_file.
        '''
        # For some reason the HDF file grows unreasonable large. These
        # commands compress the file saving a lot of disk space.
        f = 'ptrepack --chunkshape=auto --propindexes --complevel=9 --complib=blosc {} {}'
        tmp_file = '{}.tmp'.format(self.cache_file)
        self.logger.debug('Running ptrepack on %s', self.cache_file)
        ret = os.system(f.format(self.cache_file, tmp_file))
        self.logger.debug('return code = %s', ret)
        self.logger.debug('Moving tmp file back to original')
        ret = move(tmp_file, self.cache_file)
        self.logger.debug('return code = %s', ret)

    def _put_df(self, df, name, metadata=None, append_profile_key=False):
        '''Save Pandas DataFrame to local HDF file with optional metadata dict.
        '''
        store = pd.HDFStore(self.cache_file)
        self.logger.debug('Saving DataFrame to name "%s" in file %s',
                                              name, self.cache_file)
        store[name] = df
        if metadata:
            store.get_storer(name).attrs.metadata = metadata
        ##if append_profile_key and not df.empty:
        ##    store.append('profile_keys', pd.Series(name))
        self.logger.debug('store.close()')
        store.close()

    def _get_df(self, name):
        '''Get Pandas DataFrame from local HDF file or raise KeyError.
        '''
        store = pd.HDFStore(self.cache_file)
        try:
            self.logger.debug('Getting "%s" from %s', name, self.cache_file)
            df = store[name]
        except (IOError, KeyError):
            raise
        finally:
            self.logger.debug('store.close()')
            store.close()

        return df

    def _status_to_df(self):
        '''Read the data at status_url link and return it as a Pandas DataFrame.
        '''
        self.logger.info('Reading data from %s', self.status_url)
        req = requests.get(self.status_url)
        req.encoding = 'UTF-16LE'

        # Had to tell requests the encoding, StringIO makes the text 
        # look like a file object. Skip over leading BOM bytes.
        df = pd.read_csv(StringIO(req.text[1:]))
        return df

    def _global_meta_to_df(self):
        '''Read the data at global_url link and return it as a Pandas DataFrame.
        '''
        self.logger.info('Reading data from %s', self.global_url)
        with closing(urllib2.urlopen(self.global_url)) as r:
            df = pd.read_csv(r, comment='#')

        return df

    def _get_pressures(self, ds, max_pressure):
        '''From xray ds return tuple of pressures list and pres_indices list.
        '''
        pressures = []
        pres_indices = []
        for i, p in enumerate(ds['PRES_ADJUSTED'].values[0]):
            if p >= max_pressure:
                break
            pressures.append(p)
            pres_indices.append(i)

        if not pressures:
            self.logger.warn('No PRES_ADJUSTED values in netCDF file')

        return pressures, pres_indices

    def _profile_to_dataframe(self, wmo, url, max_pressure):
        '''Return a Pandas DataFrame of profiling float data from data at url.
        '''
        self.logger.debug('Opening %s', url)
        ds = xray.open_dataset(url)

        self.logger.debug('Checking %s for our desired variables', url)
        for v in self.variables:
            if v not in ds.keys():
                raise RequiredVariableNotPresent('{} not in {}'.format(v, url))

        pressures, pres_indices = self._get_pressures(ds, max_pressure)

        # Make a DataFrame with a hierarchical index for better efficiency
        # Argo data have a N_PROF dimension always of length 1, hence the [0]
        tuples = [(wmo, ds['JULD'].values[0], ds['LONGITUDE'].values[0], 
                        ds['LATITUDE'].values[0], round(pres, 1))
                                        for pres in pressures]
        df = pd.DataFrame()
        if tuples:
            indices = pd.MultiIndex.from_tuples(tuples, names=['wmo', 'time', 
                                                        'lon', 'lat', 'pressure'])
            # Add only non-coordinate variables to the DataFrame
            for v in self.variables ^ self._coordinates:
                try:
                    s = pd.Series(ds[v].values[0][pres_indices], index=indices)
                    if s.dropna().empty:
                        self.logger.warn('%s: N_PROF [0] empty, trying [1]', v)
                        try:
                            s = pd.Series(ds[v].values[1][pres_indices], index=indices)
                        except IndexError:
                            pass
                    self.logger.debug('Added %s to DataFrame', v)
                    df[v] = s
                except KeyError:
                    self.logger.warn('%s not in %s', v, url)
                except pydap.exceptions.ServerError as e:
                    self.logger.error(e)

        return df

    def _float_profile(self, url):
        '''Return last part of url: <wmo>P<profilenumber>
        '''
        regex = re.compile(r"(\d+_\d+).nc$")
        m = regex.search(url)
        return 'P{:s}'.format(m.group(1))

    def set_verbosity(self, verbosity):
        '''Change loglevel. 0: ERROR, 1: WARN, 2: INFO, 3:DEBUG.
        '''
        self.logger.setLevel(self._log_levels[verbosity])

    def get_oxy_floats_from_status(self, age_gte=340):
        '''Return a Pandas Series of floats that are identified to have oxygen,
        are not greylisted, and have an age greater or equal to age_gte. 

        Args:
            age_gte (int): Restrict to floats with data >= age, defaults to 340
        '''
        try:
            df = self._get_df(self._STATUS)
        except (IOError, KeyError):
            self.logger.debug('Could not read status from cache, loading it.')
            self._put_df(self._status_to_df(), self._STATUS)
            df = self._get_df(self._STATUS)

        odf = df.query('(OXYGEN == 1) & (GREYLIST == 0) & (AGE != 0) & '
                       '(AGE >= {:d})'.format(age_gte))

        return odf['WMO'].tolist()

    def get_dac_urls(self, desired_float_numbers):
        '''Return dictionary of Data Assembly Centers keyed by wmo number.

        Args:
            desired_float_numbers (list[str]): List of strings of float numbers
        '''
        try:
            df = self._get_df(self._GLOBAL_META)
        except KeyError:
            self.logger.debug('Could not read global_meta, putting it into cache.')
            self._put_df(self._global_meta_to_df(), self._GLOBAL_META)
            df = self._get_df(self._GLOBAL_META)

        dac_urls = {}
        for _, row in df.loc[:,['file']].iterrows():
            floatNum = row['file'].split('/')[1]
            if floatNum in desired_float_numbers:
                url = self.thredds_url
                url += '/'.join(row['file'].split('/')[:2])
                url += "/profiles/catalog.xml"
                dac_urls[floatNum] = url

        self.logger.debug('Found %s dac_urls', len(dac_urls))

        return dac_urls

    def get_profile_opendap_urls(self, catalog_url):
        '''Returns an iterable to the opendap urls for the profiles in catalog.
        The `catalog_url` is the .xml link for a directory on a THREDDS Data 
        Server.
        '''
        urls = []
        try:
            self.logger.debug("Parsing %s", catalog_url)
            req = requests.get(catalog_url)
        except ConnectionError as e:
            self.logger.error('Cannot open catalog_url = %s', catalog_url)
            self.logger.exception(e)
            return urls

        soup = BeautifulSoup(req.text, 'html.parser')

        # Expect that this is a standard TDS with dodsC used for OpenDAP
        base_url = '/'.join(catalog_url.split('/')[:4]) + '/dodsC/'

        # Pull out <dataset ... urlPath='...nc'> attributes from the XML
        for e in soup.findAll('dataset', attrs={'urlpath': re.compile("nc$")}):
            urls.append(base_url + e['urlpath'])

        return urls

    def _get_cache_file_parms(self, cache_file):
        '''Return dictionary of constraint parameters from name of fixed cache file.
        '''
        parm_dict = {}
        if self._fixed_cache_base in cache_file:
            for regex in [a for a in dir(self) if not callable(a) and 
                                                  a.endswith("RE")]:
                try:
                    p = re.compile(self.__getattribute__(regex))
                    m = p.search(cache_file)
                    parm_dict[regex[1:-2]] = int(m.group(1))
                except AttributeError:
                    pass

        return parm_dict

    def _validate_cache_file_parm(self, parm, value):
        '''Return adjusted parm value so as not to exceed fixed cache file value.
        '''
        adjusted_value = value
        cache_file_value = None
        try:
            cache_file_value = self.cache_file_parms[parm]
        except KeyError:
            # Return a ridiculously large integer to force reading all data
            adjusted_value =  10000000000
        except AttributeError:
            # No cache_file sepcified
            pass

        if value and cache_file_value:
            if value > cache_file_value:
                self.logger.warn("Requested %s %s exceeds cache file's parameter: %s",
                                  parm, value, cache_file_value)
                self.logger.info("Setting %s to %s", parm, cache_file_value)
                adjusted_value = cache_file_value
        elif not value and cache_file_value:
            self.logger.info("Using fixed cache file's %s value of %s", parm, 
                                                            cache_file_value)
            adjusted_value = cache_file_value

        if not adjusted_value:
            # Final check for value = None and not set by cache_file
            adjusted_value = 10000000000

        return adjusted_value

    def _validate_oxygen(self, df, url):
        '''Return empty DataFrame if no valid oxygen otherwise return df.
        '''
        if df['DOXY_ADJUSTED'].dropna().empty:
            self.logger.warn('Oxygen is all NaNs in %s', url)
            df = pd.DataFrame()

        return df

    def _save_profile(self, url, count, opendap_urls, wmo, key, max_pressure,
                            float_msg):
        '''Put profile data into the local HDF cache.
        '''
        try:
            self.logger.info('%s, Profile %s of %s, key = %s', 
                             float_msg, count, len(opendap_urls), key)
            df = self._profile_to_dataframe(wmo, url, max_pressure)
            if not df.empty and self._oxygen_required:
                df = self._validate_oxygen(df, url)
        except RequiredVariableNotPresent as e:
            self.logger.warn(str(e))
            df = pd.DataFrame()

        self._put_df(df, key, {'url', url}, append_profile_key=True)

        return df

    def get_float_dataframe(self, wmo_list, max_profiles=None, max_pressure=None,
                                  append_df=True):
        '''Returns Pandas DataFrame for all the profile data from wmo_list.
        Uses cached data if present, populates cache if not present.  If 
        max_profiles is set to a number then data from only those profiles
        will be returned, this is useful for testing or for getting just 
        the most recent data from the float. Set append_df to False if
        calling simply to load cache_file (reduces memory requirements).
        '''
        max_profiles = self._validate_cache_file_parm('profiles', max_profiles)
        max_pressure = self._validate_cache_file_parm('pressure', max_pressure)

        float_df = pd.DataFrame()
        for f, (wmo, dac_url) in enumerate(self.get_dac_urls(wmo_list).iteritems()):
            float_msg = 'WMO {}: Float {} of {}'. format(wmo, f+1, len(wmo_list))
            self.logger.info(float_msg)
            opendap_urls = self.get_profile_opendap_urls(dac_url)
            for i, url in enumerate(opendap_urls):
                if i > max_profiles:
                    self.logger.info('Stopping at max_profiles = %s', max_profiles)
                    break
                key = self._float_profile(url)
                try:
                    df = self._get_df(key)
                except KeyError:
                    df = self._save_profile(url, i, opendap_urls, wmo, key, 
                                            max_pressure, float_msg)

                self.logger.debug(df.head())
                if append_df:
                    float_df = float_df.append(df)

            self.logger.info('Repacking cache file')
            self._repack_hdf()

        return float_df

