#! /usr/bin/env python

# Copyright 2017, 2018 Max H. Parke KA1RBI
# 
# This file is part of OP25
# 
# OP25 is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3, or (at your option)
# any later version.
# 
# OP25 is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public
# License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with OP25; see the file COPYING. If not, write to the Free
# Software Foundation, Inc., 51 Franklin Street, Boston, MA
# 02110-1301, USA.

import sys
import os
import time
import re
import json
import socket
import traceback
import threading
import glob

from gnuradio import gr
from waitress.server import create_server
from optparse import OptionParser
from multi_rx import byteify
from rx import p25_rx_block

my_input_q = None
my_output_q = None
my_recv_q = None
my_port = None
my_backend = None
CFG_DIR = '../www/config/'

"""
fake http and ajax server module
TODO: make less fake
"""

def static_file(environ, start_response):
    content_types = { 'png': 'image/png', 'jpeg': 'image/jpeg', 'jpg': 'image/jpeg', 'gif': 'image/gif', 'css': 'text/css', 'js': 'application/javascript', 'html': 'text/html'}
    img_types = 'png jpg jpeg gif'.split()
    if environ['PATH_INFO'] == '/':
        filename = 'index.html'
    else:
        filename = re.sub(r'[^a-zA-Z0-9_.\-]', '', environ['PATH_INFO'])
    suf = filename.split('.')[-1]
    pathname = '../www/www-static'
    if suf in img_types:
        pathname = '../www/images'
    pathname = '%s/%s' % (pathname, filename)
    if suf not in content_types.keys() or '..' in filename or not os.access(pathname, os.R_OK):
        sys.stderr.write('404 %s\n' % pathname)
        status = '404 NOT FOUND'
        content_type = 'text/plain'
        output = status
    else:
        output = open(pathname).read()
        content_type = content_types[suf]
        status = '200 OK'
    return status, content_type, output

def valid_tsv(filename):
    if not os.access(filename, os.R_OK):
        return False
    line = open(filename).readline()
    for word in 'Sysname Offset NAC Modulation TGID Whitelist Blacklist'.split():
        if word not in line:
            return False
    return True

def do_request(d):
    global my_backend
    TSV_DIR = './'
    if d['command'].startswith('rx-'):
        msg = gr.message().make_from_string(json.dumps(d), -2, 0, 0)
        if not my_backend.input_q.full_p():
            my_backend.input_q.insert_tail(msg)
        return None
    elif d['command'] == 'config-load':
        filename = '%s%s.json' % (CFG_DIR, d['data'])
        if not os.access(filename, os.R_OK):
            return
        js_msg = json.loads(open(filename).read())
        return {'json_type':'config_data', 'data': js_msg}
    elif d['command'] == 'config-list':
        files = glob.glob('%s*.json' % CFG_DIR)
        files = [x.replace('.json', '') for x in files]
        files = [x.replace(CFG_DIR, '') for x in files]
        if d['data'] == 'tsv':
            tsvfiles = glob.glob('%s*.tsv' % TSV_DIR)
            tsvfiles = [x for x in tsvfiles if valid_tsv(x)]
            tsvfiles = [x.replace('.tsv', '[TSV]') for x in tsvfiles]
            tsvfiles = [x.replace(TSV_DIR, '') for x in tsvfiles]
            files += tsvfiles
        return {'json_type':'config_list', 'data': files}
    elif d['command'] == 'config-save':
        name = d['data']['name']
        if '..' in name or '.json' in name or '/' in name:
            return None
        filename = '%s%s.json' % (CFG_DIR, d['data']['name'])
        open(filename, 'w').write(json.dumps(d['data']['value'], indent=4, separators=[',',':'], sort_keys=True))
        return None

def post_req(environ, start_response, postdata):
    global my_input_q, my_output_q, my_recv_q, my_port
    resp_msg = []
    try:
        data = json.loads(postdata)
    except:
        sys.stderr.write('post_req: error processing input: %s:\n' % (postdata))
        traceback.print_exc(limit=None, file=sys.stderr)
        sys.stderr.write('*** end traceback ***\n')
    for d in data:
        if d['command'].startswith('config-') or d['command'].startswith('rx-'):
            resp = do_request(d)
            if resp:
                resp_msg.append(resp)
            continue
        msg = gr.message().make_from_string(str(d['command']), -2, d['data'], 0)
        if my_output_q.full_p():
            my_output_q.delete_head_nowait()   # ignores result
        if not my_output_q.full_p():
            my_output_q.insert_tail(msg)
    time.sleep(0.2)

    while not my_recv_q.empty_p():
        msg = my_recv_q.delete_head()
        if msg.type() == -4:
            resp_msg.append(json.loads(msg.to_string()))
    status = '200 OK'
    content_type = 'application/json'
    output = json.dumps(resp_msg)
    return status, content_type, output

def http_request(environ, start_response):
    if environ['REQUEST_METHOD'] == 'GET':
        status, content_type, output = static_file(environ, start_response)
    elif environ['REQUEST_METHOD'] == 'POST':
        postdata = environ['wsgi.input'].read()
        status, content_type, output = post_req(environ, start_response, postdata)
    else:
        status = '200 OK'
        content_type = 'text/plain'
        output = status
        sys.stderr.write('http_request: unexpected input %s\n' % environ['PATH_INFO'])
    
    response_headers = [('Content-type', content_type),
                        ('Content-Length', str(len(output)))]
    start_response(status, response_headers)

    return [output]

def application(environ, start_response):
    failed = False
    try:
        result = http_request(environ, start_response)
    except:
        failed = True
        sys.stderr.write('application: request failed:\n%s\n' % traceback.format_exc())
        sys.exit(1)
    return result

def process_qmsg(msg):
    if my_recv_q.full_p():
        my_recv_q.delete_head_nowait()   # ignores result
    if my_recv_q.full_p():
        return
    my_recv_q.insert_tail(msg)

class http_server(object):
    def __init__(self, input_q, output_q, endpoint, **kwds):
        global my_input_q, my_output_q, my_recv_q, my_port
        if endpoint == 'internal':
            return
        else:
            host, port = endpoint.split(':')
            if my_port is not None:
                raise AssertionError('this server is already active on port %s' % my_port)
        my_input_q = input_q
        my_output_q = output_q
        my_port = int(port)

        my_recv_q = gr.msg_queue(10)
        self.q_watcher = queue_watcher(my_input_q, process_qmsg)

        self.server = create_server(application, host=host, port=my_port)

    def run(self):
        self.server.run()

class queue_watcher(threading.Thread):
    def __init__(self, msgq,  callback, **kwds):
        threading.Thread.__init__ (self, **kwds)
        self.setDaemon(1)
        self.msgq = msgq
        self.callback = callback
        self.keep_running = True
        self.start()

    def run(self):
        while(self.keep_running):
            msg = self.msgq.delete_head()
            self.callback(msg)

class Backend(threading.Thread):
    def __init__(self, options, input_q, output_q, **kwds):
        threading.Thread.__init__ (self, **kwds)
        self.setDaemon(1)
        self.keep_running = True
        self.rx_options = None
        self.input_q = input_q
        self.output_q = output_q
        self.verbosity = options.verbosity
        self.start()

    def process_msg(self, msg):
        msg = json.loads(msg.to_string())
        if msg['command'] == 'rx-start':
            options = rx_options(msg['data'])
            options.verbosity = self.verbosity
            options._js_config['config-rx-data'] = {'input_q': self.input_q, 'output_q': self.output_q}
            self.tb = p25_rx_block(options)

    def run(self):
        while self.keep_running:
            msg = self.input_q.delete_head()
            self.process_msg(msg)

class rx_options(object):
    def __init__(self, name):
        def map_name(k):
            return k.replace('-', '_')

        filename = '%s%s.json' % (CFG_DIR, name)
        if not os.access(filename, os.R_OK):
            return
        config = byteify(json.loads(open(filename).read()))
        dev = [x for x in config['devices'] if x['active']][0]
	if not dev:
            return
        chan = [x for x in config['channels'] if x['active']][0]
	if not chan:
            return
        options = object()
        for k in config['backend-rx'].keys():
            setattr(self, map_name(k), config['backend-rx'][k])
        for k in 'args frequency gains offset'.split():
            setattr(self, k, dev[k])
        for k in 'demod_type filter_type'.split():
            setattr(self, k, chan[k])
        self.freq_corr = dev['ppm']
        self.sample_rate = dev['rate']
        self.plot_mode = chan['plot']
        self.phase2_tdma = chan['phase2_tdma']
        self.trunk_conf_file = None
        self.terminal_type = None
        self._js_config = config

def http_main():
    global my_backend
    # command line argument parsing
    parser = OptionParser()
    parser.add_option("-c", "--config-file", type="string", default=None, help="specify config file name")
    parser.add_option("-e", "--endpoint", type="string", default="127.0.0.1:8080", help="address:port to listen on (use addr 0.0.0.0 to enable external clients)")
    parser.add_option("-v", "--verbosity", type="int", default=0, help="message debug level")
    parser.add_option("-p", "--pause", action="store_true", default=False, help="block on startup")
    (options, args) = parser.parse_args()

    # wait for gdb
    if options.pause:
        print 'Ready for GDB to attach (pid = %d)' % (os.getpid(),)
        raw_input("Press 'Enter' to continue...")

    input_q = gr.msg_queue(20)
    output_q = gr.msg_queue(20)
    backend_input_q = gr.msg_queue(20)
    backend_output_q = gr.msg_queue(20)

    my_backend = Backend(options, backend_input_q, backend_output_q)
    server = http_server(input_q, output_q, options.endpoint)

    server.run()

if __name__ == '__main__':
    http_main()
