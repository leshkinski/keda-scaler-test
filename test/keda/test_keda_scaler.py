#!/usr/bin/env python
import datetime
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import unittest
import yaml

from opencensus.ext.azure import metrics_exporter
from opencensus.stats import aggregation as aggregation_module
from opencensus.stats import measure as measure_module
from opencensus.stats import stats as stats_module
from opencensus.stats import view as view_module

TEST_DIR = os.path.dirname(__file__)
TEST_ENV_FILE = os.path.join(TEST_DIR, '..', '..', 'deploy', 'generated', 'test-env.json')
TEST_HELM_CHART = os.path.join(TEST_DIR, '..', '..', 'helm', 'combo-scaler')
TEST_HELM_RELEASE = 'combo-scaler-test'

stats = stats_module.stats
view_manager = stats.view_manager
stats_recorder = stats.stats_recorder

APP_INSIGHTS_METRIC = 'test-app-insights-metric'
APP_INSIGHTS_ROLE = 'test-app-insights-role'

with open(TEST_ENV_FILE) as handle:
    TEST_ENV = json.load(handle)
    TEST_ENV['TIMEZONE'] = os.getenv('TIMEZONE', 'America/New_York')


def callback_set_role(envelope):
    metrics = envelope.data.baseData.metrics
    if len(metrics) > 0:
        envelope.tags['ai.cloud.role'] = APP_INSIGHTS_ROLE


class Scaler:
    def __init__(self, name: str, min_replicas: int, max_replicas: int, env: dict):
        self.name = name
        self.min_replicas = min_replicas
        self.max_replicas = max_replicas
        self.test_vars = env
        self.namespace = '-'.join([name, 'test'])
        self.image_tag = '1.14.1'

    def set_min_replicas(self, min_replicas: int):
        self.min_replicas = min_replicas
        self.test_vars['MIN_REPLICAS'] = self.min_replicas

    def helm_values(self):
        return {
            'namespace': self.namespace,
            'imageTag': self.image_tag,
            'appInsightsId': self.test_vars['APP_INSIGHTS_APP_ID'],
            'azurePrincipalId': self.test_vars['AZURE_SP_ID'],
            'azurePrincipalSecret': self.test_vars['AZURE_SP_KEY'],
            'azureTenantId': self.test_vars['AZURE_SP_TENANT'],
            'minReplicas': self.min_replicas,
            'maxReplicas': self.max_replicas,
        }


class ComboScaler(Scaler):
    def __init__(
            self, name: str, min_replicas: int, max_replicas: int, env: dict, desired_replicas: int,
            target_value: int, metric: str, role: str):
        super().__init__(name, min_replicas, max_replicas, env)
        self.desired_replicas = desired_replicas
        self.metric = metric
        self.cloud_role = role
        self.target_value = target_value
        self.measure = None
        self.mmap = None
        self.cron_duration_mins = 2

    def init_metric_exporter(self):
        self.measure = measure_module.MeasureInt(self.metric, self.metric)
        view = view_module.View(
            self.metric, self.metric, [], self.measure, aggregation_module.LastValueAggregation())
        view_manager.register_view(view)
        instrumentation_key = self.test_vars['APP_INSIGHTS_INSTRUMENTATION_KEY']
        exporter = metrics_exporter.new_metrics_exporter(
            connection_string=f'InstrumentationKey={instrumentation_key}',
            export_interval=30
        )

        exporter.add_telemetry_processor(callback_set_role)
        view_manager.register_exporter(exporter)
        self.mmap = stats_recorder.new_measurement_map()

    def set_metric(self, value):
        self.mmap.measure_int_put(self.measure, value)
        self.mmap.record()

    def helm_values(self):
        now = datetime.datetime.now()
        start_time = now - datetime.timedelta(minutes=1)
        end_time = now + datetime.timedelta(minutes=self.cron_duration_mins)
        values = super().helm_values()
        values.update({
            'desiredReplicas': self.desired_replicas,
            'timezone': self.test_vars['TIMEZONE'],
            'startMinute': start_time.minute,
            'startHour': start_time.hour,
            'endMinute': end_time.minute,
            'endHour': end_time.hour,
            'metric': self.metric,
            'role': self.cloud_role,
            'targetValue': self.target_value,
            'workspaceId': self.test_vars['LOG_ANALYTICS_WORKSPACE_ID']
        })
        return values


TEST_SCALER = ComboScaler('combo', 0, 3, TEST_ENV, 2, 10, APP_INSIGHTS_METRIC, APP_INSIGHTS_ROLE)


class TestComboScaler(unittest.TestCase):
    logger = None

    mmap = None
    tmap = None

    @classmethod
    def helm_upgrade(cls, scaler: Scaler):
        with tempfile.NamedTemporaryFile('w+t', prefix='test-scaler') as yaml_file:
            yaml.dump(scaler.helm_values(), yaml_file)
            yaml_file.flush()
            subprocess.check_call([
                'helm', 'upgrade', '--install', '--values', yaml_file.name, TEST_HELM_RELEASE, TEST_HELM_CHART
            ])

    @classmethod
    def helm_uninstall(cls):
        subprocess.check_call(['helm', 'uninstall', TEST_HELM_RELEASE])

    @classmethod
    def setUpClass(cls) -> None:
        cls.logger = logging.getLogger(name='scale-test')
        stream_handler = logging.StreamHandler(sys.stdout)
        cls.logger.setLevel(logging.INFO)
        cls.logger.addHandler(stream_handler)
        formatter = logging.Formatter('%(asctime)s %(levelname)8s: %(message)s')
        formatter.converter = time.gmtime
        stream_handler.setFormatter(formatter)

        TEST_SCALER.init_metric_exporter()
        TEST_SCALER.set_metric(0)

        cls.logger.info(f'initializing {TEST_SCALER.name}')
        result = subprocess.run(['kubectl', 'create', 'ns', TEST_SCALER.namespace], capture_output=True)
        if result.returncode != 0 and '(AlreadyExists)' not in result.stderr.decode('utf-8'):
            raise Exception(f'failed to create namespace. stderr: {result.stderr.decode("utf-8")}')

    @classmethod
    def tearDownClass(cls) -> None:
        cls.helm_uninstall()
        try:
            subprocess.check_call(['kubectl', 'delete', 'ns', TEST_SCALER.namespace])
        except subprocess.CalledProcessError:
            cls.logger.error(f'failed to delete {TEST_SCALER.namespace}')

    @classmethod
    def wait(cls, condition, wait_sec: int, fail_message: str):
        success = False
        end_time = time.time() + wait_sec
        while time.time() <= end_time:
            if condition():
                success = True
                break
            time.sleep(2)

        assert success, fail_message

    @classmethod
    def assert_replicas(cls, namespace: str, replicas: int, wait_sec: int, fail_message: str):
        def __assert_replicas():
            result = subprocess.check_output([
                'kubectl', 'get', 'deployment.apps/test-deployment', '--namespace', namespace,
                '-o', 'jsonpath="{.spec.replicas}"']).decode('utf-8')
            actual_replicas = int(result.strip('"'))
            cls.logger.info(f'replicas - expected: {replicas} actual: {actual_replicas}')
            return replicas == actual_replicas

        cls.wait(__assert_replicas, wait_sec, fail_message)

    @classmethod
    def log_test_step(cls, scaler: Scaler, msg: str):
        cls.logger.info(f'{scaler.namespace}/${scaler.name}: {msg}')

    def do_test_cron_scale_up_and_down(self, min_replicas: int):
        TEST_SCALER.set_min_replicas(min_replicas)

        min_replicas = TEST_SCALER.min_replicas
        desired_replicas = TEST_SCALER.desired_replicas

        self.helm_upgrade(TEST_SCALER)

        self.assert_replicas(
            TEST_SCALER.namespace, desired_replicas, 30,
            f'deployment should have scaled to {desired_replicas} replicas')

        wait_sec = 90 if min_replicas == 0 else 390
        start_time = datetime.datetime.now()
        self.assert_replicas(
            TEST_SCALER.namespace, min_replicas, wait_sec, f'deployment should have scaled to {min_replicas} replicas')
        elapsed_time = datetime.datetime.now() - start_time
        self.logger.info(f'scaled down in {elapsed_time.seconds} seconds')

    @unittest.skip
    def test_cron_scale_up_and_down_to_zero(self):
        self.do_test_cron_scale_up_and_down(0)

    @unittest.skip
    def test_cron_scale_up_and_down_to_nonzero(self):
        self.do_test_cron_scale_up_and_down(1)

    def test_scale_up_and_up(self):
        min_replicas = TEST_SCALER.min_replicas
        max_replicas = TEST_SCALER.max_replicas
        desired_replicas = TEST_SCALER.desired_replicas

        self.logger.info('deploying scalers')
        TEST_SCALER.cron_duration_mins = 14
        self.helm_upgrade(TEST_SCALER)

        self.logger.info('waiting for cron scheduler to activate')
        self.assert_replicas(
            TEST_SCALER.namespace, desired_replicas, 180,
            f'deployment should have scaled to {desired_replicas} replicas after cron activated')

        # max 3 minutes in
        self.logger.info('waiting for app insights scaler to activate')
        TEST_SCALER.set_metric((TEST_SCALER.target_value + 1) * (TEST_SCALER.desired_replicas + 1))
        self.assert_replicas(
            TEST_SCALER.namespace, max_replicas, 180,
            f'deployment should have {max_replicas} replicas after app insights activated')

        self.logger.info('ensure replicas unchanged after image update')
        TEST_SCALER.image_tag = '1.14.2'
        self.helm_upgrade(TEST_SCALER)
        self.assert_replicas(
            TEST_SCALER.namespace, max_replicas, 5,
            f'deployment should have {max_replicas} replicas after image update')
        time.sleep(60)
        self.assert_replicas(
            TEST_SCALER.namespace, max_replicas, 5,
            f'deployment should have {max_replicas} replicas after image update')

        # max 6 minutes in
        self.logger.info('waiting for app insights scaler to deactivate')
        TEST_SCALER.set_metric(0)
        self.assert_replicas(
            TEST_SCALER.namespace, desired_replicas, 480,
            f'deployment should have {desired_replicas} replicas after app insights deactivated')

        # max 14 minutes in
        self.logger.info('set end time to now for quicker cron scale down')
        self.logger.info('updating scalers')
        TEST_SCALER.cron_duration_mins = 1
        self.helm_upgrade(TEST_SCALER)

        self.logger.info('waiting for cron scaler to deactivate')
        self.assert_replicas(
            TEST_SCALER.namespace, min_replicas, 600,
            f'deployment should have {min_replicas} replicas after cron deactivated')


if __name__ == '__main__':
    unittest.main()
