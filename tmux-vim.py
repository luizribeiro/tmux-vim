#!/usr/bin/env python

import ConfigParser;
import os;
import pipes;
import re;
import shlex;
import subprocess;
import sys;

#------------------------------------------------------------------------------

cfg = {}

#------------------------------------------------------------------------------

def die(msg):
	print >> sys.stderr, msg
	sys.exit(1)

def load_section(parser, section):
	if not parser.has_section(section):
		return { }
	return dict(parser.items(section))

def load_layout(parser):
	layout = load_section(parser, 'layout')
	base_key = 'include'
	while base_key in layout:
		base_section = 'layout=' + layout[base_key]
		if not parser.has_section(base_section):
			die('Layout section %s not found' % (base_section))
		base_items = parser.items(base_section)
		del layout[base_key]
		layout = dict(base_items + layout.items())
	return layout

def load_config(defaults):
	parser = ConfigParser.SafeConfigParser()
	inifile = os.environ.get('TMUX_VIM_INI',
							 os.path.expanduser('~/.tmux-vim.ini'))
	try:
		parser.read(inifile)
	except ConfigParser.Error, e:
		die('Reading %s:\n%s' % (inifile, e))
	cfg = defaults
	cfg.update(load_section(parser, 'commands'))
	cfg['layout'] = load_layout(parser)
	cfg['tmux'] = shlex.split(cfg['tmux']) # we exec this directly, not via sh
	return cfg

def tmux_exec(*args):
	subprocess.check_call(cfg['tmux'] + list(args))

def check_output(command):
	process = subprocess.Popen(command, stdout=subprocess.PIPE)
	output, unused_err = process.communicate()
	retcode = process.poll()
	if retcode:
		raise subprocess.CalledProcessError(retcode, command, output=output)
	return output

def cmd_query(command, pattern):
	output = check_output(command)
	regex = re.compile(pattern, re.MULTILINE)
	match = regex.search(output)
	if match is None:
		return None
	return match.group(1)

def tmux_query(command, pattern):
	return cmd_query(cfg['tmux'] + command, pattern)

def make_pattern(lhs):
	return '^' + re.escape(lhs) + '=(.*)\s*$'

def pane_query(rhs_format, lhs_match):
	return tmux_query(
		['lsp', '-F', '#{pane_id}=#{%s}' % (rhs_format)],
		make_pattern(lhs_match)
	)

def tmux_fetch_env(key):
	return tmux_query(['show-environment'], make_pattern(key))

def tmux_store_env(key, value):
	tmux_exec('set-environment', key, value)

def tmux_window_id():
	return pane_query('window_index', os.environ['TMUX_PANE']);

def get_vim_cwd(vim_pane_id):
	vim_pid = pane_query('pane_pid', vim_pane_id)
	try:
		return cmd_query(
			[ 'lsof', '-p', vim_pid, '-a', '-d', 'cwd', '-Fn' ],
			'^n(.*)$'
		)
	except OSError:
		return None

def tmux_pane_size(split):
	if split == 'h':
		dimension = 'width'
	else:
		dimension = 'height'
	return int(pane_query('pane_' + dimension, os.environ['TMUX_PANE']))

def select_pane(pane_id):
	if pane_query('pane_id', pane_id) == None:
		return False
	cmd = cfg['tmux'] + ['select-pane', '-t', str(pane_id)]
	return subprocess.call(cmd) == 0

def layout_option(key, default):
	return cfg['layout'].get(key, default)

def split_method(vim_pos):
	if vim_pos == 'left' or vim_pos == 'right':
		return 'h'
	else:
		return 'v'

def eval_percent(pc, val):
	if str(pc)[-1] == '%':
		return int(pc[:-1]) * val / 100
	else:
		return int(pc)

def compute_layout():
	vim_pos = layout_option('vim-pos', 'right')
	split = split_method(vim_pos)
	pane = tmux_pane_size(split)
	mode = layout_option('mode', 'shell')
	swap_panes = (vim_pos == 'left' or vim_pos == 'top')
	vim_args = ' '

	default_shell = { 'h': 132, 'v': 15  }
	default_vim   = { 'h':  80, 'v': 24  }

	if mode == 'shell':

		shell_size = layout_option('size', default_shell[split])
		split_size = eval_percent(shell_size, pane)

	elif mode == 'vim':

		vim = eval_percent(layout_option('size', default_vim[split]), pane)

		# Factor in the vim sub-window count
		count = layout_option('count', 1)
		if count == 'auto':
			reserve = layout_option('reserve', default_shell[split])
			shell = eval_percent(reserve, pane)
			count = max(1, (pane - shell) / (vim + 1))
		else:
			count = int(count)

		split_size = (vim + 1) * count - 1

		autosplit = bool(layout_option('autosplit', False))
		if autosplit:
			window_method = { 'h': 'O', 'v': 'o' }
			vim_args += "-%s%d" % (window_method[split], count)

	if swap_panes:
		split_size = pane - split_size - 1

	return {
		'split_method': '-' + split,
		'split_size':	str(split_size),
		'swap_panes':	swap_panes,
		'vim_args':		vim_args,
	}


def spawn_vim_pane(filenames):
	opt = compute_layout()
	vim_files = ' '.join(map(pipes.quote, filenames))
	vim_cmd = ' '.join(['exec', cfg['vim'], opt['vim_args'], vim_files])
	tmux_cmd = cfg['tmux'] + ['split-window', '-P', opt['split_method'],
	                                          '-l', opt['split_size'],
											  vim_cmd]
	pane_path = check_output(tmux_cmd).rstrip('\n\r')

	# 0:1.1: [100x88] [history 0/10000, 0 bytes] %2
	# ^^^^^ pane_path                   pane_id  ^^
	pattern = '^' + re.escape(pane_path) + ':.*(%\\d+)'
	pane_id = tmux_query(['lsp', '-a'], pattern)

	if opt['swap_panes']:
		tmux_exec('swap-pane', '-D')

	return pane_id

def vim_command(command, filename, vim_cwd):
	# Vim or bash may have changed directory, so we need some path manipulation
	# First compute the absolute path
	path = os.path.abspath(filename)

	# Then, if we have a vim working directory, a relative path from that ...
	if vim_cwd is not None:
		relpath = os.path.relpath(filename, vim_cwd)
		# ... and choose the shorter of the two
		if len(relpath) < len(path):
			path = relpath

	# We split filename up into chars so it isn't processed as a tmux
	# key name (eg. space or enter)
	return [ ':', command, 'space' ] + list(path) + [ 'enter' ]

def reuse_vim_pane(pane_id, filenames):
	if filenames:
		vim_cwd = get_vim_cwd(pane_id)
		cmd = ['send-keys', '-t', pane_id, 'escape']
		for filename in filenames[:-1]:
			cmd += vim_command('badd', filename, vim_cwd)
		cmd += vim_command('edit', filenames[-1], vim_cwd)
		tmux_exec(*cmd)

def pre_flight_checks():
	# Check that tmux is running
	if not 'TMUX' in os.environ:
		die('tmux session not detected')

    # Check that tmux supports the -V command (>= v1.5)
	if subprocess.call(cfg['tmux'] + ['-V'], stdout=open(os.devnull), stderr=open(os.devnull)) != 0:
		die('tmux 1.6 or greater is required')

    # Check tmux is v1.6 or greater
	if float(tmux_query(['-V'], 'tmux\s+(\S+)')) < 1.6:
		die('tmux 1.6 or greater is required')

#------------------------------------------------------------------------------

def main(filenames):
	global cfg
	cfg = load_config({ 'tmux': 'tmux', 'vim': 'vim' })
	pre_flight_checks()
	window_key = 'tmux_vim_pane_' + tmux_window_id()
	vim_pane_id = tmux_fetch_env(window_key)
	if vim_pane_id is None or not select_pane(vim_pane_id):
		vim_pane_id = spawn_vim_pane(filenames)
		tmux_store_env(window_key, vim_pane_id)
	else:
		reuse_vim_pane(vim_pane_id, filenames)

#------------------------------------------------------------------------------

if __name__ == "__main__":
	main(sys.argv[1:])
