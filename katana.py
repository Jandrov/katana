#!/usr/bin/env python3

from pwn import *
import argparse
import json
import sys
import importlib
import queue
import threading
import time
import traceback
import os
import utilities
from utilities import ArgumentParserWithHelp, find_modules_recursively
import pkgutil
import re
import binascii
import base64
import units

class Katana(object):

	def __init__(self):
		self.results = {}
		self.config = {}
		self.parsers = []
		self.units = []
		self.threads = []
		self.completed = False
		self.results = { }
		self.results_lock = threading.RLock()
		self.total_work = 0
		self.blacklist = []
		self.all_units = []
		self.requested_units = []
		self.recurse_queue = []
		self.recurse_lock = threading.Lock()
		self.recurse_cond = threading.Condition(lock=self.recurse_lock)

		# Initial parser is for unit directory. We need to process this argument first,
		# so that the specified unit may be loaded
		self.parser = ArgumentParserWithHelp(
			description='Low-hanging fruit checker for CTF problems',
			add_help=False,
			allow_abbrev=False)
		self.parser.add_argument('--unitdir', type=utilities.DirectoryArgument,
			default='./units', help='the directory where available units are stored')
		self.parser.add_argument('--unit', action='append',
			required=False, default = [], help='the units to run on the targets')
		self.parser.add_argument('--unit-help', action='store_true',
			default=False, help='display help on unit selection')
		# The number of threads to use
		self.parser.add_argument('--threads', '-t', type=int, default=10,
			help='number of threads to use')
		# Whether or not to use the built-in module checks
		self.parser.add_argument('--force', '-f', action='store_true',
			default=False, help='skip the checks')
		# The list of targets to scan
		self.parser.add_argument('target', type=str,
			help='the target file/url/IP/etc') 
		# The output directory for this scan
		self.parser.add_argument('--outdir', '-o', default='./results',
			help='directory to house results')
		# A Regular Expression patter for units to match
		self.parser.add_argument('--flag-format', '-ff', default=None,
			help='regex pattern for output (e.g. "FLAG{.*}")')
		self.parser.add_argument('--auto', '-a', default=False,
			action='store_true', help='automatically search for matching units in unitdir')
		self.parser.add_argument('--depth', '-d', type=int, default=5,
				help='the maximum depth which the units may recurse')
		self.parser.add_argument('--exclude', action='append',
			required=False, default = [], help='units to exclude in a recursive case')
		self.parser.add_argument('--verbose', '-v', action='store_true',
			default=False, help='show the running threads')

		# Parse initial arguments
		self.parse_args()

		# Remove the exclusions, if we have any set
		self.blacklist += self.config['exclude']

		# We want the "-" target to signify stdin
		if len(self.original_target) == 1 and self.original_target[0] == '-':
			self.config['target'] = sys.stdin.read()

		# Compile the flag format if given
		if self.config['flag_format']:
			self.flag_pattern = re.compile('({0})'.format(self.config['flag_format']),
					flags=re.MULTILINE | re.DOTALL | re.IGNORECASE)
		else:
			self.flag_pattern = None

		# Setup the work queue
		self.work = queue.Queue(maxsize=self.config['threads']*2)

		# Insert the unit directory module into th epath
		sys.path.insert(0, self.config['unitdir'])

		# Don't run if the output directory exists
		if os.path.exists(self.config['outdir']):
			log.error('{0}: directory exists'.format(self.config['outdir']))
		elif not os.path.exists(self.config['outdir']):
			# Create the directory if needed
			try:
				os.mkdir(self.config['outdir'])
			except:
				log.error('{0}: unable to create directory'.format(self.config['outdir']))

		for importer, name, ispkg in pkgutil.walk_packages([self.config['unitdir']], ''):
			try:
				module = importlib.import_module(name)
			except ImportError:
				log.failure('{0}: failed to load module')
				traceback.print_exc()
				exit()

			# Check if this module requires dependencies
			try:
				dependencies = module.DEPENDENCIES
			except AttributeError:
				dependencies = []

			# Ensure the dependencies exist
			try:
				for dependency in dependencies:
					subprocess.check_output(['which',dependency])
			except (FileNotFoundError, subprocess.CalledProcessError): 
				pass
			else:
				# Dependencies are good, ensure the unit class exists
				try:
					unit_class = module.Unit
				except AttributeError:
					continue
			
			# Keep track of the units we asked for
			try:
				idx = self.config['unit'].index(name)
				del self.config['unit'][idx]
				self.requested_units.append(unit_class)
			except ValueError:
				pass

			# Keep total list for blind recursion
			self.all_units.append(unit_class)

		# Notify user of failed unit loads
		for unit in self.config['unit']:
			log.failure('{0}: Unit not found or failed to import')

		# Ensure we have something to do
		if len(self.requested_units) == 0 and not self.config['auto']:
			log.failure('no units loaded. aborting.')
			exit()

		# Notify the user if the requested units are overridden by recursion
		if self.config['auto'] and len(self.requested_units) > 0 and not recurse:
			log.warning('ignoring --unit options in favor of --auto')

		# Find units which match this target
		self.units = self.locate_units(self.config['target'])

	@property
	def original_target(self):
		""" Shorthand for grabbing the target """
		return self.config['target']	

	def add_results(self, unit, d):
		""" Update the results dict with the given dict """
		parents = unit.family_tree
		with self.results_lock:
			# Start at the global results
			r = self.results
			# Recurse through parent units
			for p in parents:
				# If we have not seen results from this parent,
				# THAT'S FINE.... just be ready for it
				if not p.unit_name in r:
					r[p.unit_name] = { 'results': [] }	
			if unit.unit_name not in r:
				r[unit.unit_name] = { 'results': [] }

			if d != {}:
				r[unit.unit_name]['results'].append(d)


	def evaluate(self):
		""" Start processing all units """

		prog = log.progress('katana')

		prog.status('starting threads')

		# Create all the threads
		for n in range(self.config['threads']):
			prog.status('starting thread {0}'.format(n))
			thread = threading.Thread(target=self.worker)
			thread.start()
			self.threads.append(thread)

		prog.status('filling work queue')

		status_done = threading.Event()
		status_thread = threading.Thread(target=self.progress, args=(prog,status_done))
		status_thread.start()

		# Add the known units to the work queue
		self.add_to_work(self.units)

		# Monitor the work queue and update the progress
		# while True:
		# 	# Grab the numer of items in the queue
		# 	n = self.work.qsize()
		# 	# End if we are done
		# 	if n == 0:
		# 		break
		# 	# Print a nice percentage compelte
		# 	prog.status('{0:.2f}% complete'.format((self.total_work-float(n)) / float(self.total_work)))
		# 	# We want to give the threads time to execute
		# 	time.sleep(0.5)

		self.work.join()

		status_done.set()
		status_thread.join()

		prog.status('all units complete. waiting for thread exit')

		# Notify threads of completion
		for n in range(self.config['threads']):
			self.work.put((None, None, None))

		# Wait for threads to exit
		for t in self.threads:
			t.join()

		# Make sure we can create the results file
		with open(os.path.join(self.config['outdir'], 'katana.json'), 'w') as f:
			json.dump(self.results, f, indent=4, sort_keys=True)

		prog.success('threads exited. evaluation complete')

		log.success('wrote output summary to {0}'.format(os.path.join(self.config['outdir'], 'katana.json')))

	def add_to_work(self, units):
		# Add all the cases to the work queue
		for unit in units:
			self.work.put((unit,name,unit.enumerate(self)))
			# if not self.completed:
			# 	case_no = 0
			# 	for case in unit.enumerate(self):
			# 		if not unit.completed:
			# 			#prog.status('adding {0}[{1}] to work queue (size: {2}, total: {3})'.format(
			# 			#	unit.unit_name, case_no, self.work.qsize(), self.total_work
			# 			#))
			# 			self.work.put((unit, case_no, case))
			# 			self.total_work += 1
			# 			case_no += 1
			# 		else:
			# 			break


	def add_flag(self, flag):
		if 'flags' not in self.results:
			self.results['flags'] = []
		with self.results_lock:
			if flag not in self.results['flags']:
				log.success('Found flag: {0}'.format(flag))
				self.results['flags'].append(flag)
	
	def locate_flags(self, unit, output, stop=True):
		""" Look for flags in the given data/output """

		# If the user didn't supply a pattern, there's nothing to do.
		if self.flag_pattern == None:
			return False

		match = self.flag_pattern.search(output)
		if match:
			self.add_flag(match.group())
			
			# Stop the unit if they asked
			if stop:
				unit.completed = True

			return True

		return False

	def recurse(self, unit, data):
		# JOHN: If this `recurse` is set to True, it will recurse 
		#       WITH EVERYTHING even IF you specify a single unit.
		#       This is the intent, but should be left to "False" for testing
		
		if (data is None or data == "" ):
			return
		
		# Obey max depth input by user
	

		if len(unit.family_tree) >= self.config['depth']:
			log.warning('depth limit reached. if this is a recursive problem, consider increasing --depth')
			# Stop the chain of events
			unit.completed = True
			return

		try:
			log.info('starting for {0}'.format(data))
			units = self.locate_units(data, parent=unit, recurse=True)
			self.add_to_work(units)
			log.info('done for {0}'.format(data))
		except:
			traceback.print_exc()


	def load_unit(self, target, name, required=True, recurse=True, parent=None):
		
		required = False

		# This unit is not compatible with the system (previous dependancy error)
		if name in self.blacklist:
			return

		try:
			# import the module
			module = importlib.import_module(name)

			# We don't load units from packages
			if module.__name__ != module.__package__:
				unit_class = None

				# Check if this module requires dependencies
				try:
					dependencies = module.DEPENDENCIES
					for dependency in dependencies:
						try:
							subprocess.check_output(['which',dependency])
						except (FileNotFoundError, subprocess.CalledProcessError): 
							raise units.DependancyError(dependency)

				except AttributeError:
					pass

				# Try to grab the unit class. Fail if it doesn't exit
				try:
					unit_class = module.Unit
				except AttributeError:
					if required:
						log.info('{0}: no Unit class found'.format(module.__name__))

				# Climb the family tree to see if ANY ancester is not allowed to recurse..
				# If that is the case, don't bother with this unit
				if unit_class.PROTECTED_RECURSE and parent is not None:
					for p in ([ parent ] + parent.family_tree):
						if p.PROTECTED_RECURSE:
							if required:
								log.info('{0}: PROTECTED_RECURSE set. cannot recurse into {1}'.format(
									parent.unit_name,
									name
								))
							raise units.NotApplicable

				unit = unit_class(self, parent, target)
				if parent is not None and unit.parent is None:
					print("this should never happen")


				yield unit

			# JOHN: This is what runs if just pass --unit ...
			elif recurse:
				# Load children, if there are any
				for importer, name, ispkg in pkgutil.walk_packages(module.__path__, module.__name__+'.'):
					for unit in self.load_unit(target, name, required=False, recurse=False):
						yield unit

		except ImportError as e:
			if required:
				traceback.print_exc()
				log.failure('unit {0} does not exist'.format(name))
				exit()

		except units.NotApplicable as e:
			if required:
				raise e

		except units.DependancyError as e:
			log.failure('{0}: failed due to missing dependancy: {1}'.format(
				name, e.dependancy
			))
			self.blacklist.append(name)
		except Exception as e:
			# if required: # This should ALWAYS print....
			traceback.print_exc()
			log.failure('unknown error when loading {0}: {1}'.format(name, e))
			exit()
	

	def locate_units(self, target, parent=None, recurse=False):

		units_so_far = []

		if not self.config['auto'] and not recurse:
			for unit_class in self.requested_units:
				try:
					units_so_far.append(unit_class(self, parent, target))
				except units.NotApplicable:
					log.failure('{0}: unit not applicable to target'.format(
						unit.__module__.__name__
					))
		else:
			for unit_class in self.all_units:
				try:
					# Climb the family tree to see if ANY ancester is not allowed to recurse..
					# If that is the case, don't bother with this unit
					if unit_class.PROTECTED_RECURSE and parent is not None:
						for p in ([ parent ] + parent.family_tree):
							if p.PROTECTED_RECURSE:
								raise units.NotApplicable
					units_so_far.append(unit_class(self, parent, target))
				except units.NotApplicable:
					pass

		return units_so_far

		# JOHN: This is what runs if you pass `-a`...
		if not self.config['auto'] and not recurse:
			# Load explicit units
			for unit in self.config['unit']:
				try:
					for current_unit in self.load_unit(target, unit, required=True, recurse=True, parent=parent):
						units_so_far.append(current_unit)
				except units.NotApplicable:
					# If this unit is NotApplicable, don't try it!
					pass
		else:
			if self.config['auto'] and len(self.config['unit']) > 0 and not recurse:
				log.warning('ignoring --unit options in favor of --auto')

			# Iterate through all `.py` files in the unitdir directory
			# Grab everything that has a unit, and check if it's valid.
			# if it is, add it to the unit list.
			for importer,name,ispkg in pkgutil.walk_packages([self.config['unitdir']], ''):
				try:
					for current_unit in self.load_unit(target, name, required=False, recurse=False, parent=parent):
						# print("adding unit", current_unit)
						units_so_far.append(current_unit)
				except units.NotApplicable as e:
					# If this unit is NotApplicable, don't try it!
					pass

		return units_so_far

	def add_argument(self, *args, **kwargs):
		""" Add an argument to the argument parser """

		try:
			self.parser.add_argument(*args, **kwargs)
		except argparse.ArgumentError as e:
			return e
	
		return None

	def parse_args(self, final=False):
		""" Use the given parser to parse the remaining arguments """

		# Parse the arguments
		args, remaining = self.parser.parse_known_args()

		# Update the configuration
		self.config.update(vars(args))

		return self.config
	
	# Build an argument parser for katana
	def ArgumentParser(self, *args, **kwargs):
		return argparse.ArgumentParser(parents=self.parsers, add_help = False, *args, **kwargs)

	def progress(self, progress, done_event):
		while not done_event.is_set():
			if self.total_work > 0:
				left = self.work.qsize()
				done = self.total_work - left
				progress.status('{0:.2f}% work queue utilization; {1} total items in queued; {2} completed'.format((float(done)/float(self.total_work))*100, self.total_work, done))
			time.sleep(0.1)

	def worker(self):
		""" Katana worker thread to process unit execution """

		if self.config['verbose']:
			progress = log.progress('thread-{0} '.format(threading.get_ident()))
		else:
			progress = None

		while True:
			# Grab the next item
			unit,name,gen = self.work.get()

			# The boss says NO. STAHP.
			if unit is None and gen is None and name is None:
					break

			if unit.completed:
				self.work.task_done()
				continue

			# Grab the next case
			try:
				case = next(gen)
			except StopIteration:
				continue

			# Put that back, there may be more
			self.work.put((unit,name,gen))

			# Perform the evaluation
			if progress is not None:
				progress.status('entering {0}'.format(unit.unit_name))
			try:
				result = unit.evaluate(self, case)
			except:
				traceback.print_exc()
			if progress is not None:
				progress.status('exiting {0}'.format(unit.unit_name))

			# Notify boss that we are done
			self.work.task_done()


# Make sure we find the local packages (first current directory)
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.getcwd())

if __name__ == '__main__':

	# Create the katana
	katana = Katana()

	# Run katana against all units
	katana.evaluate()

	# Cleanly display the results of each unit to the screen
	final_output = json.dumps(katana.results, indent=4, sort_keys=True)
	print(final_output)

	if len(final_output) > 1000:
		# Dump the flags we found
		if 'flags' in katana.results:
			for flag in katana.results['flags']:
				log.success('Found flag: {0}'.format(flag))
