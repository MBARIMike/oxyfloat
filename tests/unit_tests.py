#!/usr/bin/env python

import os
import sys
import unittest
parentDir = os.path.join(os.path.dirname(__file__), "../")
sys.path.insert(0, parentDir)

from oxyfloat import ArgoData
from oxyfloat import utils

class DataTest(unittest.TestCase):
    def setUp(self):
        self.ad = ArgoData(verbosity=2)
        self.good_oga_floats = ['1900650']
        self.bad_oga_floats = ['1901158']

    def test_get_oxyfloats(self):
        self.oga_floats = self.ad.get_oxy_floats_from_status()
        self.assertNotEqual(len(self.oga_floats), 0)

    def _get_dac_urls(self):
        # Testing with a float that has data
        for dac_url in self.ad.get_dac_urls(self.good_oga_floats).values():
            self.dac_url = dac_url
            self.assertTrue(self.dac_url.startswith('http'))
            break

    def _get_profile_opendap_urls(self):
        for profile_url in self.ad.get_profile_opendap_urls(self.dac_url):
            self.profile_url = profile_url
            break

    def _profile_to_dataframe(self):
        d = self.ad._profile_to_dataframe(self.good_oga_floats[0], 
                self.profile_url, 11000)
        self.assertNotEqual(len(d), 0)

    def test_read_profile_data(self):
        # Methods need to be called in order
        self._get_dac_urls()
        self._get_profile_opendap_urls()
        self._profile_to_dataframe()

    def test_get_float_dataframe(self):
        df = self.ad.get_float_dataframe(self.good_oga_floats, max_profiles=2)
        self.assertNotEqual(len(df), 0)
        df = self.ad.get_float_dataframe(self.bad_oga_floats, max_profiles=2)
        self.assertEqual(len(df), 0)

    def test_cache_file(self):
        ad = ArgoData(cache_file='/tmp/oxyfloat_cache_file.hdf')
        ad.set_verbosity(1)

    def test_fixed_cache_file(self):
        age = 3000      # Returns 1 float on 2 November 2015
        parent_dir = os.path.join(os.path.dirname(__file__), "../")

        # Simulated what's done by load_cache.py
        from scripts.load_cache import ArgoDataLoader
        from argparse import Namespace
        adl = ArgoDataLoader()
        adl.args = Namespace(age=3000, profiles=1)
        cache_file = os.path.abspath(
                     os.path.join(parent_dir, 'oxyfloat', adl.short_cache_file()))

        ad = ArgoData(verbosity=2, cache_file=cache_file)
        wmo_list = ad.get_oxy_floats_from_status(age_gte=age)
        # Force limiting to what's in cache_file name: 1
        ad.get_float_dataframe(wmo_list, max_profiles=2)
        # Force using maximum value
        ad.get_float_dataframe(wmo_list)

    def test_util_o2sat(self):
        # See http://www.engineeringtoolbox.com/oxygen-solubility-water-d_841.html
        self.assertAlmostEqual(utils.o2sat(35, 5), 308, places=0)
        self.assertAlmostEqual(utils.o2sat(35, 20), 225, places=0)
        self.assertAlmostEqual(utils.o2sat(35, 30), 190, places=0)

    def test_util_convert_to_mll(self):
        # See http://www.engineeringtoolbox.com/oxygen-solubility-water-d_841.html
        # and http://www.oceanographers.net/forums/showthread.php?1486-ask-how-to-conversion-ml-L-to-%B5mol-kg
        self.assertAlmostEqual(utils.convert_to_mll(308, 35, 5, 0), 7.1, places=1)
        self.assertAlmostEqual(utils.convert_to_mll(225.6, 36.5, 1, 0), 5.2, places=1)
        
if __name__ == '__main__':
    unittest.main()
