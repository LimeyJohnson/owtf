#!/usr/bin/env python
"""

owtf is an OWASP+PTES-focused try to unite great tools and facilitate pen testing
Copyright (c) 2011, Abraham Aranguren <name.surname@gmail.com> Twitter: @7a_ http://7-a.org
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:
    * Redistributions of source code must retain the above copyright
      notice, this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright
      notice, this list of conditions and the following disclaimer in the
      documentation and/or other materials provided with the distribution.
    * Neither the name of the copyright owner nor the
      names of its contributors may be used to endorse or promote products
      derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.  Manager Process

"""

import os
import sys
import time
import signal
import subprocess
import logging
import multiprocessing
from framework.lib.owtf_process import OWTFProcess
from framework.lib.exceptions import InvalidWorkerReference


class Worker(OWTFProcess):
    def pseudo_run(self):
        """
        When run for the first time, put something into output queue ;)
        """
        self.output_q.put('Started')
        while self.poison_q.empty():
            try:
                work = self.input_q.get()
                # If work is empty this means no work is there
                if work == ():
                    exit(0)
                target, plugin = work
                pluginDir = self.core.PluginHandler.GetPluginGroupDir(
                    plugin['group'])
                self.core.PluginHandler.SwitchToTarget(target["id"])
                self.core.PluginHandler.ProcessPlugin(pluginDir, plugin)
                self.output_q.put('done')
            except KeyboardInterrupt:
                logging.debug(
                    "I am worker (%d) & my master doesn't need me anymore",
                    self.pid)
                exit(0)
            except Exception, e:
                self.core.Error.LogError(
                    "Exception occured while running :",
                    trace=str(e))
        logging.debug(
            "I am worker (%d) & my master gave me poison pill",
            self.pid)
        exit(0)


class WorkerManager(object):
    def __init__(self, CoreObj):
        self.core = CoreObj
        self.worklist = []  # List of unprocessed (plugin*target)
        self.workers = []  # list of worker and work (worker, work)
        self.spawn_workers()

    def get_allowed_process_count(self):
        process_per_core = int(self.core.DB.Config.Get('PROCESS_PER_CORE'))
        cpu_count = multiprocessing.cpu_count()
        return(process_per_core*cpu_count)

    def get_task(self):
        work = None
        free_mem = self.core.Shell.shell_exec(
            "free -m | grep Mem | sed 's/  */#/g' | cut -f 4 -d#")
        if int(free_mem) > int(self.core.DB.Config.Get('MIN_RAM_NEEDED')):
            work = self.core.DB.Worklist.get_work(self.targets_in_use())
        else:
            logging.warn("Not enough memory to execute a plugin")
        return work

    def spawn_workers(self):
        """
        This function spawns the worker process and give them initial work
        """
        # Check if maximum limit of processes has reached
        while (len(self.workers) < self.get_allowed_process_count()):
            self.spawn_worker()
        if not len(self.workers):
            self.core.Error.FrameworkAbort(
                "Zero worker processes created because of lack of memory")

    def spawn_worker(self, index=None):
        w = Worker(
            self.core,
            input_q=multiprocessing.Queue(),
            output_q=multiprocessing.Queue())
        worker_dict = {
            "worker": w,
            "work": (),
            "busy": False,
            "paused": False
        }

        if index is not None:
            logging.debug("Replacing worker at index %d" % (index))
            self.workers[index] = worker_dict
        else:
            logging.debug("Adding a new worker")
            self.workers.append(worker_dict)
        w.start()

    def targets_in_use(self):
        target_ids = []
        for item in self.workers:
            try:
                target_ids.append(item["work"][0]["id"])
            except IndexError:
                continue
        return target_ids

    def manage_workers(self):
        """
        This function manages workers, it polls on each queue of worker
        checks if it has done his work and then gives it new work
        if there is one
        """
        # Loop while there is some work in worklist
        work_in_progress = False
        for k in range(0, len(self.workers)):
            if (not self.workers[k]["worker"].output_q.empty()) or (not self.workers[k]["worker"].is_alive()):
                if self.workers[k]["worker"].is_alive():
                    # Assign target, plugin from tuple work and empty the tuple
                    self.workers[k]["work"] = ()
                    self.workers[k]["busy"] = False  # Worker is IDLE
                else:
                    logging.info("Worker with name %s and pid %s seems dead" % (
                        self.workers[k]["worker"].name,
                        self.workers[k]["worker"].pid))
                    self.spawn_worker(index=k)
                work_to_assign = self.get_task()
                if work_to_assign:
                    logging.info("Work assigned to %s with pid %d" % (
                        self.workers[k]["worker"].name,
                        self.workers[k]["worker"].pid))
                    trash_can = self.workers[k]["worker"].output_q.get()
                    # Assign work ,set target to used,and process to busy
                    self.workers[k]["worker"].input_q.put(work_to_assign)
                    self.workers[k]["work"] = work_to_assign
                    self.workers[k]["busy"] = True
            work_in_progress = work_in_progress or self.workers[k]["busy"]
        if (self.core.Config.QuitOnCompletion and not work_in_progress) and \
                (self.core.DB.Worklist.get_total_work_count() == 0):
            self.core.Finish()

    def poison_pill_to_workers(self):
        """
        This function waits for each worker to complete his work and
        send it Poision Pill(emtpy work)
        """
        for item in self.workers:
            # Check if process is doing some work
            if item["busy"]:
                if item["paused"]:
                    self._signal_process(item["worker"].pid, signal.SIGCONT)
                trash = item["worker"].output_q.get()
                item["busy"] = False
                item["work"] = ()
            item["worker"].poison_q.put("DIE")

    def join_workers(self):
        """
        Joins all the workers
        """
        for item in self.workers:
            item["worker"].join()

    def clean_up(self):
        self.poison_pill_to_workers()
        self.join_workers()

    def exit(self):
        """
        This function empties the pending work list and aborts all processes
        """
        # As worklist is emptied, aborting of plugins will result in
        # killing of workers
        self.worklist = []  # It is a list
        for item in self.workers:
            work = item["worker"].poison_q.put("DIE")
            self._signal_process(item["worker"].pid, signal.SIGINT)

    def _signal_process(self, pid, psignal):
        """
        This function kills all children of a process and abort that process
        """
        # Child processes are handled at shell level :P
        # self.core.KillChildProcesses(pid,signal.SIGINT)
        try:
            # This will kick the exception handler if plugin is running,
            # so plugin is killed
            # Else, the worker dies :'(
            os.kill(pid, psignal)
        except Exception, e:
            logging.error(
                "Error while trying to abort Worker process",
                exc_info=True)

    def _signal_children(self, parent_pid, psignal):
        ps_command = subprocess.Popen(
            "ps -o pid --ppid %d --noheaders" % parent_pid,
            shell=True,
            stdout=subprocess.PIPE)
        ps_output = ps_command.stdout.read()
        for pid_str in ps_output.split("\n")[:-1]:
            self._signal_children(int(pid_str), psignal)
            try:
                os.kill(int(pid_str), psignal)
            except Exception:
                logging.error("Error while trying to signal", exc_info=True)

# --------------------------- API Methods ---------------------------- #
# PSEUDO_INDEX = INDEX + 1

    def get_worker_details(self, pseudo_index=None):
        if pseudo_index:
            try:
                temp_dict = dict(self.workers[pseudo_index-1])
                temp_dict["worker"] = temp_dict["worker"].pid
                temp_dict["id"] = pseudo_index
                return(temp_dict)
            except IndexError:
                raise InvalidWorkerReference(
                    "No worker process with id: " + str(pseudo_index))
        else:
            worker_temp_list = []
            for i in range(0, len(self.workers)):
                temp_dict = dict(self.workers[i])
                temp_dict["worker"] = temp_dict["worker"].pid
                temp_dict["id"] = i+1  # Zero-Index is not human friendly
                worker_temp_list.append(temp_dict)
            return(worker_temp_list)

    def get_worker_dict(self, pseudo_index):
        try:
            return(self.workers[pseudo_index-1])
        except IndexError:
            raise InvalidWorkerReference(
                "No worker process with id: " + str(pseudo_index))

    def create_worker(self):
        """
        Create new worker
        """
        self.spawn_worker()

    def delete_worker(self, pseudo_index):
        """
        This actually deletes the worker :
        + Send SIGINT to the worker
        + Remove it from self.workers so that is is not restarted by
          manager cron
        """
        worker_dict = self.get_worker_dict(pseudo_index)
        if not worker_dict["busy"]:
            self._signal_process(worker_dict["worker"].pid, signal.SIGINT)
            del self.workers[pseudo_index-1]
        else:
            raise InvalidWorkerReference(
                "Worker with id " + str(pseudo_index) + " is busy")

    def pause_worker(self, pseudo_index):
        """
        Pause worker by sending SIGSTOP after verifying the process is running
        """
        worker_dict = self.get_worker_dict(pseudo_index)
        if not worker_dict["paused"]:
            self._signal_children(worker_dict["worker"].pid, signal.SIGSTOP)
            self._signal_process(worker_dict["worker"].pid, signal.SIGSTOP)
            worker_dict["paused"] = True

    def resume_worker(self, pseudo_index):
        """
        Resume worker by sending SIGCONT after verfifying that process is paused
        """
        worker_dict = self.get_worker_dict(pseudo_index)
        if worker_dict["paused"]:
            self._signal_children(worker_dict["worker"].pid, signal.SIGCONT)
            self._signal_process(worker_dict["worker"].pid, signal.SIGCONT)
            worker_dict["paused"] = False

    def abort_worker(self, pseudo_index):
        """
        Abort worker i.e kill current command, but the worker process is not
        removed, so manager_cron will restart it
        """
        worker_dict = self.get_worker_dict(pseudo_index)
        # You only send SIGINT to worker since it will handle it more
        # gracefully and kick the command process's ***
        self._signal_process(worker_dict["worker"].pid, signal.SIGINT)
