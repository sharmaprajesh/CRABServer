#!/usr/bin/python
# pylint: disable=line-too-long
"""

"""
from __future__ import division
from __future__ import print_function
import json
import logging
import threading
import os
import subprocess

import fts3.rest.client.easy as fts3
from datetime import timedelta
from RESTInteractions import HTTPRequests, CRABRest
from httplib import HTTPException
from ServerUtilities import encodeRequest
from TransferInterface import CRABDataInjector

FTS_ENDPOINT = "https://fts3-cms.cern.ch:8446/"
FTS_MONITORING = "https://fts3-cms.cern.ch:8449/"

if not os.path.exists('task_process/transfers'):
    os.makedirs('task_process/transfers')

logging.basicConfig(
    filename='task_process/transfers/transfer_inject.log',
    level=logging.INFO,
    format='%(asctime)s[%(relativeCreated)6d]%(threadName)s: %(message)s'
)

if os.path.exists('task_process/RestInfoForFileTransfers.json'):
    with open('task_process/RestInfoForFileTransfers.json') as fp:
        restInfo = json.load(fp)
        proxy = os.getcwd() + "/" + str(restInfo['proxyfile'])  # make sure no to unicode to FTS clients
        #rest_filetransfers = restInfo['host'] + '/crabserver/' + restInfo['dbInstance']
        os.environ["X509_USER_PROXY"] = proxy

#if os.path.exists('task_process/rest_filetransfers.txt'):
#    with open("task_process/rest_filetransfers.txt", "r") as _rest:
#        rest_filetransfers = _rest.readline().split('\n')[0]
#        proxy = os.getcwd() + "/" + _rest.readline()
#        print("Proxy: %s" % proxy)

if os.path.exists('USE_NEW_PUBLISHER'):
    asoworker = 'schedd'
else:
    asoworker = 'asoless'

if os.path.exists('USE_FTS_REUSE'):
    ftsReuse = True
else:
    ftsReuse = False

def chunks(l, n):
    """
    Yield successive n-sized chunks from l.
    :param l: list to splitt in chunks
    :param n: chunk size
    :return: yield the next list chunk
    """
    for i in range(0, len(l), n):
        yield l[i:i + n]


def mark_transferred(ids, crabserver):
    """
    Mark the list of files as tranferred
    :param ids: list of Oracle file ids to update
    :param crabserver: an HTTPRequest object for doing POST to CRAB server REST
    :return: 0 success, 1 failure
    """
    try:
        logging.debug("Marking done %s", ids)

        data = dict()
        data['asoworker'] = asoworker
        data['subresource'] = 'updateTransfers'
        data['list_of_ids'] = ids
        data['list_of_transfer_state'] = ["DONE" for _ in ids]

        crabserver.post('/filetransfers',
                      data=encodeRequest(data))
        logging.info("Marked good %s", ids)
    except Exception:
        logging.exception("Error updating documents")
        return 1
    return 0


def mark_failed(ids, failures_reasons, crabserver):
    """
    Mark the list of files as failed
    :param ids: list of Oracle file ids to update
    :param failures_reasons: list of strings with transfer failure messages
    :param crabserver: an HTTPRequest object for doing POST to CRAB server REST
    :return: 0 success, 1 failure
    """
    try:
        data = dict()
        data['asoworker'] = asoworker
        data['subresource'] = 'updateTransfers'
        data['list_of_ids'] = ids
        data['list_of_transfer_state'] = ["FAILED" for _ in ids]
        data['list_of_failure_reason'] = failures_reasons
        data['list_of_retry_value'] = [0 for _ in ids]

        crabserver.post('/filetransfers',
                      data=encodeRequest(data))
        logging.info("Marked failed %s", ids)
    except Exception:
        logging.exception("Error updating documents")
        return 1
    return 0


def remove_files_in_bkg(pfns, logFile, timeout=None):
    """
    fork a process to remove the indicated PFN's without
    wainting for it to complete and w/o any error checking
    gfal-rm output is added to logFile
    A timeout is applied on the gfal-rm command anyhow as a sanity measure
        against runaway processes, we accept that some file may not be deleted.
    :param pfns: list of SURL's
    :param logFile: name of the logFile
    :param timeout: timeout as a string valid as arg. for linux timeout command, default is 4 hours
    :return: none
    """

    if not timeout:
        timeout = '%dm' % (len(pfns) * 3)    # default is 3minutes per file to be removed
    command = 'env -i X509_USER_PROXY=%s timeout %s gfal-rm -v -t 180 %s >> %s 2>&1 &'  % \
              (proxy, timeout, pfns, logFile)
    logging.debug("Running remove command %s", command)
    subprocess.call(command, shell=True)

    return


class check_states_thread(threading.Thread):
    """
    get transfers state per jobid
    """
    def __init__(self, threadLock, log, fts, jobid, jobs_ongoing, done_id, failed_id, failed_reasons):
        """

        :param threadLock:
        :param log:
        :param fts:
        :param jobid:
        :param jobs_ongoing:
        :param done_id:
        :param failed_id:
        :param failed_reasons:
        """
        threading.Thread.__init__(self)
        self.fts = fts
        self.jobid = jobid
        self.jobs_ongoing = jobs_ongoing
        self.log = log
        self.threadLock = threadLock
        self.done_id = done_id
        self.failed_id = failed_id
        self.failed_reasons = failed_reasons

    def run(self):
        """
        - check if the fts job is in final state (FINISHED, FINISHEDDIRTY, CANCELED, FAILED)
        - get file transfers states and get corresponding oracle ID from FTS file metadata
        - update states on oracle
        """

        self.threadLock.acquire()
        self.log.info("Getting state of job %s" % self.jobid)

        self.jobs_ongoing.append(self.jobid)

        try:
            status = self.fts.get("jobs/"+self.jobid)[0]
        except HTTPException as hte:
            self.log.exception("failed to retrieve status for %s " % self.jobid)
            self.log.exception("httpExeption headers %s " % hte.headers)
            if hte.status == 404:
                self.log.exception("%s not found in FTS3 DB" % self.jobid)
                self.jobs_ongoing.remove(self.jobid)
            return
        except Exception:
            self.log.exception("failed to retrieve status for %s " % self.jobid)
            self.threadLock.release()
            return

        self.log.info("State of job %s: %s" % (self.jobid, status["job_state"]))

        # TODO: if in final state get with list_files=True and the update_states
        if status["job_state"] in ['FINISHED', 'FINISHEDDIRTY', "FAILED", "CANCELED"]:
            file_statuses = self.fts.get("jobs/%s/files" % self.jobid)[0]

            self.done_id[self.jobid] = []
            self.failed_id[self.jobid] = []
            self.failed_reasons[self.jobid] = []
            files_to_remove = []

            for file_status in file_statuses:
                _id = file_status['file_metadata']['oracleId']
                tx_state = file_status['file_state']

                if tx_state == 'FINISHED':
                    self.done_id[self.jobid].append(_id)
                else:
                    self.failed_id[self.jobid].append(_id)
                    if file_status['reason']:
                        self.log.info('Failure reason: ' + file_status['reason'])
                        self.failed_reasons[self.jobid].append(file_status['reason'])
                    else:
                        self.log.exception('Failure reason not found')
                        self.failed_reasons[self.jobid].append('unable to get failure reason')
                files_to_remove.append(file_status['source_surl'])
            try:
                list_of_surls = ''   # gfal commands take list of SURL as a list of blank-separated strings
                for f in files_to_remove:
                    list_of_surls += str(f) + ' '  # convert JSON u'srm://....' to plain srm://...
                removeLogFile = './task_process/transfers/remove_files.log'
                remove_files_in_bkg(list_of_surls, removeLogFile)
            except Exception:
                self.log.exception('Failed to remove temp files')

        self.threadLock.release()


class submit_thread(threading.Thread):
    """Thread for actual FTS job submission

    """

    def __init__(self, threadLock, log, ftsContext, files, source, jobids, toUpdate):
        """
        :param threadLock: lock for the thread
        :param log: log object
        :param ftsContext: FTS context
        :param files: [
               [source_pfn,
                dest_pfn,
                file oracle id,
                source site,
                username,
                taskname,
                file size],
               ...]
        :param source: source site name
        :param jobids: collect the list of job ids when submitted
        :param toUpdate: list of oracle ids to update
        """
        threading.Thread.__init__(self)
        self.log = log
        self.threadLock = threadLock
        self.files = files
        self.source = source
        self.jobids = jobids
        self.ftsContext = ftsContext
        self.toUpdate = toUpdate


    def run(self):
        """

        """

        self.threadLock.acquire()
        self.log.info("Processing transfers from: %s" % self.source)

        # create destination and source pfns for job
        transfers = []
        for lfn in self.files:
            transfers.append(fts3.new_transfer(lfn[0],
                                               lfn[1],
                                               filesize=lfn[6],
                                               metadata={'oracleId': lfn[2]}
                                               )
                             )
        self.log.info("Submitting %s transfers to FTS server" % len(self.files))

        # Submit fts job
        job = fts3.new_job(transfers,
                           overwrite=True,
                           verify_checksum=True,
                           metadata={"issuer": "ASO",
                                     "userDN": self.files[0][4],
                                     "taskname": self.files[0][5]},
                           copy_pin_lifetime=-1,
                           bring_online=None,
                           source_spacetoken=None,
                           spacetoken=None,
                           # max time for job in the FTS queue in hours. From FTS experts in
                           # https://cern.service-now.com/service-portal?id=ticket&table=incident&n=INC2776329
                           # The max_time_in_queue applies per job, not per retry.
                           # The max_time_in_queue is a timeout for how much the job can stay in
                           # a SUBMITTED, ACTIVE or STAGING state.
                           # When a job's max_time_in_queue is reached, the job and all of its
                           # transfers not yet in a terminal state are marked as CANCELED
                           # StefanoB: I see that we hit this at times with 6h, causing job resubmissions,
                           # so will try to make it longer to give FTS maximum chances within our
                           # 24h overall limit (which takes care also of non-FTS related issues !)
                           # ASO transfers never require STAGING so jobs can spend max_time_in_queue only
                           # as SUBMITTED (aka queued) or ACTIVE (at least one transfer has been activated)
                           max_time_in_queue=10,
                           # from same cern.service-now.com ticket as above:
                           # The number of retries applies to each transfer within that job.
                           # A transfer is granted the first execution + number_of_retries.
                           # E.g.: retry=3 --> first execution + 3 retries
                           # so retry=3 means each transfer has 4 chances at most during the 6h
                           # max_time_in_queue
                           retry=3,
                           reuse=ftsReuse,
                           # seconds after which the transfer is retried
                           # this is a transfer that fails, gets put to SUBMITTED right away,
                           # but the scheduler will avoid it until NOW() > last_retry_finish_time + retry_delay
                           # reduced under FTS suggestion w.r.t. the 3hrs of asov1
                           # StefanoB: indeed 10 minutes makes much more sense for storage server glitches
                           retry_delay=600
                           # timeout on the single transfer process
                           # TODO: not clear if we may need it
                           # timeout = 1300
                           )

        jobid = fts3.submit(self.ftsContext, job)

        self.jobids.append(jobid)

        # TODO: manage exception here, what we should do?
        fileDoc = dict()
        fileDoc['asoworker'] = asoworker
        fileDoc['subresource'] = 'updateTransfers'
        fileDoc['list_of_ids'] = [x[2] for x in self.files]
        fileDoc['list_of_transfer_state'] = ["SUBMITTED" for _ in self.files]
        fileDoc['list_of_fts_instance'] = [FTS_ENDPOINT for _ in self.files]
        fileDoc['list_of_fts_id'] = [jobid for _ in self.files]

        self.log.info("Marking submitted %s files" % (len(fileDoc['list_of_ids'])))

        self.toUpdate.append(fileDoc)
        self.threadLock.release()


def submit(rucioClient, ftsContext, toTrans, crabserver):
    """
    submit tranfer jobs

    - group files to be transferred by source site
    - prepare jobs chunks of max 200 transfers
    - submit fts job

    :param ftsContext: fts client ftsContext
    :param toTrans: [[source pfn,
                      destination pfn,
                      oracle file id,
                      source site,
                      destination,
                      username,
                      taskname,
                      filesize, checksum],....]
    :param crabserver: an HTTPRequest object for doing POST to CRAB server REST
    :return: list of jobids submitted
    """
    threadLock = threading.Lock()
    threads = []
    jobids = []
    to_update = []

    sources = list(set([x[3] for x in toTrans]))

    for source in sources:

        ids = [x[2] for x in toTrans if x[3] == source]
        sizes = [x[7] for x in toTrans if x[3] == source]
        checksums = [x[8] for x in toTrans if x[3] == source]
        username = toTrans[0][5]
        scope = "user."+username
        taskname = toTrans[0][6]
        src_lfns = [x[0] for x in toTrans if x[3] == source]
        dst_lfns = [x[1] for x in toTrans if x[3] == source]

        sorted_source_pfns = []
        sorted_dest_pfns = []

        try:
            for chunk in chunks(src_lfns, 10):
                rucio_chunk = [scope+":"+x for x in chunk]
                # 'read' operation should better work here !
                unsorted_source_pfns = [[k.split(scope+":")[1], str(x)] for k, x in \
                                        rucioClient.cli.lfns2pfns(source, rucio_chunk, operation='read').items()]
                #logging.info(unsorted_source_pfns)
                for order_lfn in chunk:
                    for lfn, pfn in unsorted_source_pfns:
                        if order_lfn == lfn:
                            sorted_source_pfns.append(pfn)
                            break

            for chunk in chunks(dst_lfns, 10):
                rucio_chunk = [scope+":"+x for x in chunk]
                # try 'write' but fall back to 'read' if the site only implemented one
                try:
                    unsorted_dest_pfns = [[k.split(scope+":")[1], str(x)] for k, x in \
                                      rucioClient.cli.lfns2pfns(toTrans[0][4], rucio_chunk, operation='write').items()]
                except Exception as ex:
                    unsorted_dest_pfns = [[k.split(scope+":")[1], str(x)] for k, x in \
                                      rucioClient.cli.lfns2pfns(toTrans[0][4], rucio_chunk, operation='read').items()]
                #logging.info(unsorted_dest_pfns)
                for order_lfn in chunk:
                    for lfn, pfn in unsorted_dest_pfns:
                        if order_lfn == lfn:
                            sorted_dest_pfns.append(pfn)
                            break
        except Exception as ex:
            logging.error("Failed to map lfns to pfns: %s", ex)
            mark_failed(ids, ["Failed to map lfn to pfn: " + str(ex) for _ in ids], crabserver)

        source_pfns = sorted_source_pfns
        dest_pfns = sorted_dest_pfns

        tx_from_source = [[x[0], x[1], x[2], source, username, taskname, x[3], x[4]['adler32'].rjust(8,'0')] for x in zip(source_pfns, dest_pfns, ids, sizes, checksums)]

        xfersPerFTSJob = 50 if ftsReuse else 200
        for files in chunks(tx_from_source, xfersPerFTSJob):
            thread = submit_thread(threadLock, logging, ftsContext, files, source, jobids, to_update)
            thread.start()
            threads.append(thread)

    for t in threads:
        t.join()

    for fileDoc in to_update:
        _ = crabserver.post('/filetransfers',
                          data=encodeRequest(fileDoc))
        logging.info("Marked submitted %s files", fileDoc['list_of_ids'])

    return jobids


def perform_transfers(inputFile, lastLine, _lastFile, ftsContext, rucioClient, crabserver):
    """
    get transfers and update last read line number

    :param inputFile: path to the file with list of files to be transferred
    :param lastLine: number of the last line processed
    :param _last: path to the file keeping track of the last read line
    :param ftsContext: FTS context
    :param rucioClient: a Rucio Client object
    :return:
    """

    transfers = []
    logging.info("starting from line: %s", lastLine)

    with open(inputFile) as _list:
        for _data in _list.readlines()[lastLine:]:
            try:
                lastLine += 1
                doc = json.loads(_data)
            except Exception:
                continue
            transfers.append([doc["source_lfn"],
                              doc["destination_lfn"],
                              doc["id"],
                              doc["source"],
                              doc["destination"],
                              doc["username"],
                              doc["taskname"],
                              doc["filesize"],
                              doc["checksums"]])

        jobids = []
        if len(transfers) > 0:
            jobids = submit(rucioClient, ftsContext, transfers, crabserver)

            for jobid in jobids:
                logging.info("Monitor link: " + FTS_MONITORING + "fts3/ftsmon/#/job/%s", jobid)  # pylint: disable=logging-not-lazy

            # TODO: send to dashboard

        _lastFile.write(str(lastLine))

    return transfers, jobids


def state_manager(fts, crabserver):
    """

    """
    threadLock = threading.Lock()
    threads = []
    jobs_done = []
    jobs_ongoing = []
    failed_id = {}
    failed_reasons = {}
    done_id = {}

    # TODO: puo esser utile togliere questo file? mmm forse no

    if os.path.exists('task_process/transfers/fts_jobids.txt'):
        with open("task_process/transfers/fts_jobids.txt", "r") as _jobids:
            lines = _jobids.readlines()
            for line in list(set(lines)):
                if line:
                    jobid = line.split('\n')[0]
                if jobid:
                    thread = check_states_thread(threadLock, logging, fts, jobid, jobs_ongoing, done_id, failed_id, failed_reasons)
                    thread.start()
                    threads.append(thread)
            _jobids.close()

        for t in threads:
            t.join()

        try:
            for jobID, _ in done_id.items():
                logging.info('Marking job %s files done and %s files failed for job %s', len(done_id[jobID]), len(failed_id[jobID]), jobID)

                if len(done_id[jobID]) > 0:
                    doneReady = mark_transferred(done_id[jobID], crabserver)
                else:
                    doneReady = 0
                if len(failed_id[jobID]) > 0:
                    failedReady = mark_failed(failed_id[jobID], failed_reasons[jobID], crabserver)
                else:
                    failedReady = 0

                if doneReady == 0 and failedReady == 0:
                    jobs_done.append(jobID)
                    jobs_ongoing.remove(jobID)
                else:
                    jobs_ongoing.append(jobID)
        except Exception:
            logging.exception('Failed to update states')
    else:
        logging.warning('No FTS job ID to monitor yet')

    with open("task_process/transfers/fts_jobids_new.txt", "w+") as _jobids:
        for line in list(set(jobs_ongoing)):
            logging.info("Writing: %s", line)
            _jobids.write(line+"\n")

    os.rename("task_process/transfers/fts_jobids_new.txt", "task_process/transfers/fts_jobids.txt")

    return jobs_ongoing


def submission_manager(rucioClient, ftsContext, crabserver):
    """

    """
    last_line = 0
    if os.path.exists('task_process/transfers/last_transfer.txt'):
        with open("task_process/transfers/last_transfer.txt", "r") as _last:
            read = _last.readline()
            last_line = int(read)
            logging.info("last line is: %s", last_line)
            _last.close()

    # TODO: if the following fails check not to leave a corrupted file
    with open("task_process/transfers/last_transfer_new.txt", "w+") as _last:
        _, jobids = perform_transfers("task_process/transfers.txt", last_line, _last, ftsContext, rucioClient, crabserver)
        _last.close()
        os.rename("task_process/transfers/last_transfer_new.txt", "task_process/transfers/last_transfer.txt")

    with open("task_process/transfers/fts_jobids.txt", "a") as _jobids:
        for job in jobids:
            _jobids.write(str(job)+"\n")
        _jobids.close()

    return jobids


def algorithm():
    """

    script algorithm
    - create fts REST HTTPRequest
    - delegate user proxy to fts if needed
    - check for fts jobs to monitor and update states in oracle
    - get last line from last_transfer.txt
    - gather list of file to transfers
        + group by source
        + submit ftsjob and save fts jobid
        + update info in oracle
    - append new fts job ids to fts_jobids.txt
    """

    # TODO: pass by configuration
    fts = HTTPRequests(hostname=FTS_ENDPOINT.split("https://")[1],
                       localcert=proxy, localkey=proxy)

    logging.info("using user's proxy from %s", proxy)
    ftsContext = fts3.Context(FTS_ENDPOINT, proxy, proxy, verify=True)
    logging.info("Delegating proxy to FTS...")
    delegationId = fts3.delegate(ftsContext, lifetime=timedelta(hours=48), delegate_when_lifetime_lt=timedelta(hours=24), force=False)
    delegationStatus = fts.get("delegation/"+delegationId)
    logging.info("Delegated proxy valid until %s", delegationStatus[0]['termination_time'])

    # instantiate an object to talk with CRAB REST server

    try:
        crabserver = CRABRest(restInfo['host'], localcert=proxy, localkey=proxy,
                              userAgent='CRABSchedd')
        crabserver.setDbInstance(restInfo['dbInstance'])
    except Exception:
        logging.exception("Failed to set connection to crabserver")
        return

    with open("task_process/transfers.txt") as _list:
        _data = _list.readlines()[0]
        try:
            doc = json.loads(_data)
            username = doc["username"]
            taskname = doc["taskname"]
            destination = doc["destination"]
        except Exception as ex:
            msg = "Username gathering failed with\n%s" % str(ex)
            logging.warn(msg)
            raise ex

    try:
        logging.info("Initializing Rucio client")
        os.environ["X509_USER_PROXY"] = proxy
        logging.info("Initializing Rucio client for %s", taskname)
        rucioClient = CRABDataInjector(taskname,
                                    destination,
                                    account=username,
                                    scope="user."+username,
                                    auth_type='x509_proxy')
    except Exception as exc:
        msg = "Rucio initialization failed with\n%s" % str(exc)
        logging.warn(msg)
        raise exc

    jobs_ongoing = state_manager(fts, crabserver)
    new_jobs = submission_manager(rucioClient, ftsContext, crabserver)

    logging.info("Transfer jobs ongoing: %s, new: %s ", jobs_ongoing, new_jobs)

    return


if __name__ == "__main__":
    try:
        algorithm()
    except Exception:
        logging.exception("error during main loop")
    logging.info("transfer_inject.py exiting")

