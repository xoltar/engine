#!/usr/bin/env python

import os
import glob
import json
import time
import docker
import hashlib
import requests
import datetime
import traceback

import logging
logging.getLogger('requests').setLevel(logging.WARNING)
logging.basicConfig(  # root logger
    level=logging.INFO,
    format='%(asctime)s %(name)12.12s:%(levelname)4.4s %(message)s',
    datefmt='%y-%m-%d %H:%M:%S',
)
log = logging.getLogger(__name__)

import tempdir as tempfile


# TODO: convert first implementation into a super simple single threaded application
# kofalt will deal with how to run multiple simultaneously, etc, etc.

class EngineError(Exception):

    """
    Exception generated by the Engine.

    Parameters
    ----------
    message : str
        error message
    log_level : logging level object
        logging level object, e.g. logging.ERROR [default None]

    """

    def __init__(self, message, log_level=None):
        super(EngineError, self).__init__(message)
        if log_level is not None:
            message = '%s\n%s' % (message, traceback.format_exc())


class Engine(object):

    """Simple."""

    def __init__(self, api_url, _id, ssl_cert, docker_client, verify=False, tempdir=None, local=False, data_path=None, query=None, dnr=False):
        super(Engine, self).__init__()
        # configuration shits
        self.api_url = api_url
        self._id = _id
        self.headers = {'User-Agent': 'SciTran Engine %s' % self._id}
        self.ssl_cert = ssl_cert
        self.docker_client = docker_client
        self.data_path = data_path
        self.tempdir = tempdir
        self.local = local
        self.query = query
        self.group = None
        self.project = None
        self.verify = verify
        self.dnr = dnr  # do not remove

        # state
        self.job = None
        self.image_id  = None   # what image to create container from
        self.status = None      # percent complete?
        self.activity = None    # name of stage, any sort of string feedback
        self.halted = False

    def halt(self):
        log.info('Engine recieved halt - stopping')
        self.halted = True

    def run(self):
        while not self.halted:
            # reset state and all that
            self.job = None
            self.image_id = None
            self.status = None
            self.activity = None
            self.check_in()         # sets self.job
            if not self.job:
                log.info('waiting for work')
                time.sleep(10)  # no job
                continue
            self.fetch_app()        # sets self.image_id
            if not self.image_id:
                log.error('could not load or download app')
                time.sleep(10)
                self.status = 'Failed'
                self.activity = 'could not load or download app %s' % self.job.get('app_id')
            else:
                # download the files
                with tempfile.TemporaryDirectory() as tempdir_path:
                    log.debug('workin in %s' % tempdir_path)
                    input_path = os.path.join(tempdir_path, 'input')
                    output_path = os.path.join(tempdir_path, 'output')
                    meta_path = os.path.join(tempdir_path, 'meta')
                    os.makedirs(input_path)
                    os.makedirs(output_path)
                    os.makedirs(os.path.join(meta_path, 'input'))
                    os.makedirs(os.path.join(meta_path, 'output'))
                    self.fetch_inputs(tempdir_path)
                    # TODO: when in local mode, must provide the sym link end point to the container,
                    # such that the symlink path leads the correct file while inside of the container
                    # input_files.append(os.path.basename(input_file))
                    self.binds = {
                        '/scratch': {'bind': '/scratch', 'ro': True},
                        input_path: {'bind': '/input', 'ro': False},
                        output_path: {'bind': '/output', 'ro': False},
                        meta_path: {'bind': '/meta', 'ro': False},
                    }
                    exit_code = self.run_container()
                    if exit_code != 0:
                        log.error('container had non-zero exit code, %d' % exit_code)
                        self.status = 'Failed'
                        self.activity = 'Failed'
                    else:
                        self.outputs = glob.glob(os.path.join(output_path, '*'))
                        if self.outputs:
                            self.submit_results()
                            self.status = 'Done'
                            self.activity = 'generated %s' % (str(map(os.path.basename, self.outputs)))
                        else:
                            self.status = 'Failed'
                            self.activity = 'no files were generated'
                    if not self.dnr:
                        self.remove_app_container()
            self.update_job()
            log.info('JOB %6d - %s - %s/%s, %s %s' % (self.job['_id'], self.job['app']['_id'], self.job['group'], self.job['project']['name'], self.status, self.activity))

    def check_in(self):
        """
        Request work and parse the job specification.

        tears apart the job specificication to determine as much information as possible
        what application to run
        what data to run the application on
        how to run the application on the data.
        where to put the data, how to tag the completed data
        """
        # TODO write the job spec json into the bind mount for container's /input
        self.job = None
        payload = {
            'group': self.group,
            'project': self.project,
        }
        route = '%s/%s/%s' % (self.api_url, 'jobs', 'next')
        log.debug('requesting job from %s' % route)
        r = requests.get(route, headers=self.headers, data=json.dumps(payload), cert=self.ssl_cert, verify=self.verify)
        if r.status_code != 200:
            log.warning('HTTP %d: %s' % (r.status_code, r.reason))
        try:
            self.job = r.json()
        except ValueError:  # ValueEr = no json detected,
            pass
        else:
            log.debug(self.job)
            log.info('JOB %6d - %s - %s/%s, %s %s' % (self.job['_id'], self.job['app']['_id'], self.job['group'], self.job['project']['name'], self.status, self.activity))

    def fetch_app(self):
        """Prepare a docker image."""
        app_id = self.job.get('app').get('_id')
        log.debug('checking for existing docker image, %s' % app_id)
        name, tag = app_id.split(':')
        candidates = self.docker_client.images(name=name)
        for i in candidates:
            if app_id in i.get('RepoTags'):
                self.image_id = i.get('Id')
                log.debug('docker image found. uid %s' % self.image_id)
                break
        else:
            log.debug('docker image not found, requesting build context from API')
            r = requests.get('%s/%s' % (self.api_url, 'apps'), headers=self.headers, cert=self.ssl_cert)
            if r.status_code != 200:
                log.debug('download and build image not implemented')
            self.image_id = None

    def fetch_inputs(self, tempdir):
        log.debug('fetching inputs...')
        self.input_files = []  # build up
        for f in self.job.get('inputs'):
            route = '%s/%s' % (self.api_url, f.get('url'))
            fspec = f.get('payload')
            # TODO harmonize the non-local handling
            r = requests.get(route, data=json.dumps(fspec), headers=self.headers, cert=self.ssl_cert, verify=self.verify)
            if r.status_code != 200:
                raise EngineError('%s, %s\n %s' % (r.status_code, r.reason, r.content))

            fname = r.headers.get('Content-Disposition').replace('attachment; filename=', '')
            fpath = os.path.join(tempdir, 'input', fname)
            with open(fpath, 'wb') as f:
                f.write(r.content)
            if os.path.exists(fpath):
                log.debug('%s downloaded' % fpath)
            self.input_files.append(fpath)
            # return '/scratch', fpath
        self.command = ' '.join(map(os.path.basename, self.input_files))

    def run_container(self):
        self.vols = ['/input', '/output', '/meta', '/scratch'],
        app_id = self.job.get('app').get('_id')
        log.debug('creating %s container, with volumes %s' % (app_id, str(self.vols)))
        app_container = docker_client.create_container(
            image=app_id,
            volumes=self.vols,
            command=self.command,
        )
        self.app_container_id = app_container['Id']  # dictionary describing container, maybe return less info?
        log.debug('starting container %s with host volumes %s' % (self.app_container_id, str(self.binds)))
        log.debug('job start at: ' + str(datetime.datetime.now()))
        self.docker_client.start(self.app_container_id, binds=self.binds)
        for l in self.docker_client.logs(self.app_container_id, stdout=True, stderr=False, stream=True, timestamps=False):
            log.debug(l.strip('\n'))

        exit_code = self.docker_client.wait(self.app_container_id, 1)  # possibly cannot mix/match multiple blocking iterators
        log.debug('job complete at: ' + str(datetime.datetime.now()))
        return exit_code


    def submit_results(self):
        """Multi part form encoded submission."""
        log.debug('constructing multipart/form-encoded upload.')
        form_data = {}
        meta_data = []  # list of dicts
        sha_data = []   # list of dicts
        for f in self.outputs:
            fn = os.path.basename(f)
            if fn.endswith('.nii.gz'):
                fname, _, _ = fn.rsplit('.', 2)
                fext = '.nii.gz'
            else:
                fname, fext = os.path.splitext(fn)
            hash_ = hashlib.sha1()
            form_data.update({fn: (fn, open(f, 'rb'), 'application/octet-stream')})
            with open(f, 'rb') as fd:
                for chunk in iter(lambda: fd.read(2**20), ''):  # don't load the whole file at once
                    hash_.update(chunk)
            vspec = None
            for varietal in self.job.get('outputs'):  # iterate over possible ouputs
                if fext == varietal.get('payload').get('fext'):
                    vspec = varietal.get('payload')
                    log.debug('%s%s is type: %s, kinds: %s' % (fname, fext, vspec.get('type'), vspec.get('kinds')))
                    break  # use first match
            else:
                log.warning('%s%s extension did not match an expected output' % (fname, fext))
            sha = hash_.hexdigest()
            meta_data.append({
                'name': fname,
                'ext': fext,
                'kinds': vspec.get('kinds'),
                'state': vspec.get('state'),
                'type': vspec.get('type'),
                'sha1': sha,
                'size': os.path.getsize(f),
                'flavor': 'file',
            })
            sha_data.append({'name': fname+fext, 'sha1': sha})
        meta_json = json.dumps(meta_data)
        sha_data.append({'metadata': hashlib.sha1(meta_json).hexdigest()})
        sha_json = json.dumps(sha_data)
        log.info(meta_json)
        log.info(sha_json)
        log.info(form_data)
        form_data.update({'metadata': meta_json, 'sha': sha_json})

        route = '%s/%s' % (self.api_url, self.job.get('outputs')[0].get('url'))
        r = requests.put(route, files=form_data, headers=self.headers, cert=self.ssl_cert, verify=self.verify)
        if r.status_code != 200:
            raise EngineError('%d, %s' % (r.status_code, r.reason))

    def update_job(self):
        """Update a job."""
        log.debug('updating job status')
        payload = {
            'status': self.status,
            'activity': self.activity,
        }
        r = requests.put('%s/%s/%s' % (self.api_url, 'jobs', self.job.get('_id')), data=json.dumps(payload), headers=self.headers, cert=self.ssl_cert, verify=self.verify)
        if r.status_code != 200:
            raise EngineError('%d, %s' % (r.status_code, r.reason))

    def remove_app_container(self):
        log.debug('removing container: %s' % self.app_container_id)
        self.docker_client.remove_container(self.app_container_id, v=True)


if __name__ == '__main__':
    import sys
    import signal
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument('api', help='target api, example: https://www.example.com/api')
    ap.add_argument('engine_id', help='engine identification.')
    ap.add_argument('ssl_cert', help='path to SSL certificate')
    ap.add_argument('--no_verify', help='do not attempt SSL verification of API', action='store_true', default=False)
    ap.add_argument('--docker_api', help='tcp or unix socket to docker api', default='unix://var/run/docker.sock')
    ap.add_argument('--query', help='query')
    ap.add_argument('--local_mode', help='local access to data (NFS/mount)', action='store_true', default=False)
    ap.add_argument('--data_path', help='local path to data, required if local_mode enabled.')
    ap.add_argument('--log_level', help='log level (default: info)', default='info')
    ap.add_argument('--no_remove', help='do not remove containers after job stops.', action='store_true', default=False)
    args = ap.parse_args()

    # re-configure logging
    log = logging.getLogger('engine')
    log.setLevel(getattr(logging, args.log_level.upper()))

    # bad arg combinations
    if args.local_mode and not args.data_path:
        log.error('local mode (--local_mode) requires data path (--data_path)')
        sys.exit(1)
    if not args.local_mode and args.data_path:
        log.warning('data path (--data_path) set without local mode, ignoring data_path')
        args.data_path = None

    # docker client
    docker_client = docker.Client(args.docker_api)

    # silence requests InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)

    # engine
    engine = Engine(args.api, args.engine_id, args.ssl_cert, docker_client, verify=not args.no_verify, dnr=args.no_remove)

    # signal handling
    def term_handler(signum, stack):  # catch ^+U
        """Catch and handle ^+U."""
        engine.halt()
        log.info('Recieved SIGTERM - shutting down')
    signal.signal(signal.SIGTERM, term_handler)

    def int_handler(signum, stack):  # catch ^+C
        """Catch and handler ^+C."""
        engine.halt()
        log.info('Recieve SIGINT - gracefully shutting down')
    signal.signal(signal.SIGINT, int_handler)

    # do the damn thing
    engine.run()
    log.warning('Engine halted')
