# Test suite. This code could be in a separate file

from shutil import rmtree
from os.path import dirname, join
from datetime import datetime

import unittest, tempfile, json, io
from mimetypes import guess_type
from urllib.parse import urlparse, parse_qs
from httmock import HTTMock, response
from mock import Mock, patch

from ..compat import quote
from .. import util, ci, LocalProcessedResult, __version__

class TestUtilities (unittest.TestCase):

    def test_db_kwargs(self):
        '''
        '''
        dsn1 = 'postgres://who@where.kitchen/what'
        kwargs1 = util.prepare_db_kwargs(dsn1)
        self.assertEqual(kwargs1['user'], 'who')
        self.assertIsNone(kwargs1['password'])
        self.assertEqual(kwargs1['host'], 'where.kitchen')
        self.assertIsNone(kwargs1['port'])
        self.assertEqual(kwargs1['database'], 'what')
        self.assertNotIn('sslmode', kwargs1)

        dsn2 = 'postgres://who:open-sesame@where.kitchen:5432/what?sslmode=require'
        kwargs2 = util.prepare_db_kwargs(dsn2)
        self.assertEqual(kwargs2['user'], 'who')
        self.assertEqual(kwargs2['password'], 'open-sesame')
        self.assertEqual(kwargs2['host'], 'where.kitchen')
        self.assertEqual(kwargs2['port'], 5432)
        self.assertEqual(kwargs2['database'], 'what')
        self.assertEqual(kwargs2['sslmode'], 'require')
    
    def test_autoscale(self):
        '''
        '''
        autoscale, cloudwatch, as_group = Mock(), Mock(), Mock()
        group_name = 'CI Workers {0}.x'.format(*__version__.split('.'))
        
        cloudwatch.get_metric_statistics.return_value = [{}]
        autoscale.get_all_groups.return_value = [as_group]
        
        as_group.desired_capacity = 2
        util.set_autoscale_capacity(autoscale, cloudwatch, 1)
        
        # The right group name was used.
        autoscale.get_all_groups.assert_called_once_with([group_name])
        
        # Conditions haven't yet required a capacity increase.
        as_group.set_capacity.assert_not_called()

        as_group.desired_capacity = 1
        util.set_autoscale_capacity(autoscale, cloudwatch, 1)
        
        as_group.desired_capacity = 0
        cloudwatch.get_metric_statistics.return_value = [{'Maximum': 0}]
        util.set_autoscale_capacity(autoscale, cloudwatch, 1)
        
        cloudwatch.get_metric_statistics.return_value = [{'Maximum': 1}]
        util.set_autoscale_capacity(autoscale, cloudwatch, 1)
        
        # Capacity had to be increased to 1.
        as_group.set_capacity.assert_called_once_with(1)

        as_group.desired_capacity = 1
        util.set_autoscale_capacity(autoscale, cloudwatch, 2)
        
        # Capacity had to be increased to 2.
        as_group.set_capacity.assert_called_with(2)
    
    def test_task_instance(self):
        '''
        '''
        autoscale, ec2 = Mock(), Mock()
        group, config, image = Mock(), Mock(), Mock()
        keypair, reservation, instance = Mock(), Mock(), Mock()
        
        chef_role = 'good-times'
        command = 'openaddr-good-times', '--yo', 'b', 'd\\d', 'a"a', "s's", 'a:a'
        
        expected_group_name = 'CI Workers {0}.x'.format(*__version__.split('.'))
        expected_instance_name = 'Scheduled {} {}'.format(datetime.now().strftime('%Y-%m-%d'), command[0])
        
        autoscale.get_all_groups.return_value = [group]
        autoscale.get_all_launch_configurations.return_value = [config]
        ec2.aws_access_key_id, ec2.aws_secret_access_key = 'this', 'that'
        ec2.get_all_images.return_value = [image]
        ec2.get_all_key_pairs.return_value = [keypair]
        
        image.run.return_value = reservation
        reservation.instances = [instance]
        
        util.request_task_instance(ec2, autoscale, 'm3.medium', chef_role, 60, command, 'bucket-name', None)
        
        autoscale.get_all_groups.assert_called_once_with([expected_group_name])
        autoscale.get_all_launch_configurations.assert_called_once_with(names=[group.launch_config_name])
        ec2.get_all_images.assert_called_once_with(image_ids=[config.image_id])
        ec2.get_all_key_pairs.assert_called_once_with()
        
        image_run_kwargs = image.run.mock_calls[0][2]
        self.assertEqual(image_run_kwargs['instance_type'], 'm3.medium')
        self.assertEqual(image_run_kwargs['instance_initiated_shutdown_behavior'], 'terminate')
        self.assertEqual(image_run_kwargs['key_name'], keypair.name)
        
        self.assertIn('chef/run.sh {}'.format(quote(chef_role)), image_run_kwargs['user_data'])
        self.assertIn('s3://bucket-name/logs/', image_run_kwargs['user_data'])
        self.assertIn('AWS_ACCESS_KEY_ID={}'.format(quote(ec2.aws_access_key_id)), image_run_kwargs['user_data'])
        self.assertIn('AWS_SECRET_ACCESS_KEY={}'.format(quote(ec2.aws_secret_access_key)), image_run_kwargs['user_data'])
        for (arg1, arg2) in zip(command, command[1:]):
            self.assertIn(quote(arg1)+' '+quote(arg2), image_run_kwargs['user_data'])

        instance.add_tag.assert_called_once_with('Name', expected_instance_name)
    
    def test_summarize_result_licenses(self):
        '''
        '''
        s1 = {'license': 'ODbL', 'attribution name': 'ABC Co.'}
        s2 = {'website': 'http://example.com', 'attribution flag': 'false'}
        s3 = {'attribution flag': 'true', 'attribution name': ''}
        r1 = LocalProcessedResult('abc', 'abc.zip', ci.objects.RunState(s1), None)
        r2 = LocalProcessedResult('def', 'def.zip', ci.objects.RunState(s2), None)
        r3 = LocalProcessedResult('ghi', 'ghi.zip', ci.objects.RunState(s3), None)
        
        content = util.summarize_result_licenses((r1, r2, r3))
        
        self.assertIn('abc\nWebsite: Unknown\nLicense: ODbL\nRequired attribution: ABC Co.\n', content)
        self.assertIn('def\nWebsite: http://example.com\nLicense: Unknown\nRequired attribution: No\n', content)
        self.assertIn('ghi\nWebsite: Unknown\nLicense: Unknown\nRequired attribution: Yes\n', content)
    
    def test_request_ftp_file(self):
        '''
        '''
        data_sources = [
            # Two working cases based on real data
            (join(dirname(__file__), 'data', 'us-or-portland.zip'), 'ftp://ftp02.portlandoregon.gov/CivicApps/address.zip'),
            (join(dirname(__file__), 'data', 'us-ut-excerpt.zip'), 'ftp://ftp.agrc.utah.gov/UtahSGID_Vector/UTM12_NAD83/LOCATION/UnpackagedData/AddressPoints/_Statewide/AddressPoints_shp.zip'),
            
            # Some additional special cases
            (None, 'ftp://ftp02.portlandoregon.gov/CivicApps/address-fake.zip'),
            (None, 'ftp://username:password@ftp02.portlandoregon.gov/CivicApps/address-fake.zip'),
            ]
        
        for (zip_path, ftp_url) in data_sources:
            parsed = urlparse(ftp_url)

            with patch('ftplib.FTP') as FTP:
                if zip_path is None:
                    zip_bytes = None
                else:
                    with open(zip_path, 'rb') as zip_file:
                        zip_bytes = zip_file.read()
        
                cb_file = io.BytesIO()
                FTP.return_value.retrbinary.side_effect = lambda cmd, cb: cb_file.write(zip_bytes)

                with patch('openaddr.util.build_request_ftp_file_callback') as build_request_ftp_file_callback:
                    build_request_ftp_file_callback.return_value = cb_file, None
                    resp = util.request_ftp_file(ftp_url)
        
                FTP.assert_called_once_with(parsed.hostname)
                FTP.return_value.login.assert_called_once_with(parsed.username, parsed.password)
                FTP.return_value.retrbinary.assert_called_once_with('RETR {}'.format(parsed.path), None)
                
                if zip_bytes is None:
                    self.assertEqual(resp.status_code, 400, 'Nothing to return means failure')
                else:
                    self.assertEqual(resp.status_code, 200)
                    self.assertEqual(resp.content, zip_bytes, 'Expected number of bytes')
