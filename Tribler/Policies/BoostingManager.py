# -*- coding: utf-8 -*-
# Written by Egbert Bouman, Mihai Capotă, Elric Milon, and Ardhi Putra Pratama H
"""Manage boosting of swarms"""
import glob
import logging
import os
import shutil
from binascii import hexlify, unhexlify
from twisted.internet import defer, reactor
from twisted.python import log

import libtorrent as lt
import time
from twisted.internet.task import LoopingCall

from Tribler.Core.DownloadConfig import DownloadStartupConfig, DefaultDownloadStartupConfig
from Tribler.Core.Libtorrent.LibtorrentDownloadImpl import LibtorrentDownloadImpl
from Tribler.Core.Utilities import utilities
from Tribler.Core.exceptions import OperationNotPossibleAtRuntimeException
from Tribler.Core.simpledefs import DLSTATUS_SEEDING, NTFY_TORRENTS, NTFY_UPDATE, NTFY_CHANNELCAST
from Tribler.Policies.BoostingPolicy import SeederRatioPolicy
from Tribler.Policies.BoostingSource import ChannelSource
from Tribler.Policies.BoostingSource import DirectorySource
from Tribler.Policies.BoostingSource import RSSFeedSource
from Tribler.Policies.credit_mining_util import source_to_string, string_to_source, compare_torrents, \
    validate_source_string
from Tribler.Policies.defs import SAVED_ATTR, CREDIT_MINING_FOLDER_DOWNLOAD, CONFIG_KEY_ARCHIVELIST, \
    CONFIG_KEY_SOURCELIST, CONFIG_KEY_ENABLEDLIST, CONFIG_KEY_DISABLEDLIST
from Tribler.dispersy.taskmanager import TaskManager



class BoostingSettings(object):
    """
    This class contains settings used by the boosting manager
    """
    def __init__(self, session, policy=SeederRatioPolicy, load_config=True):
        self.session = session

        # Configurable parameter (changeable in runtime -plus sources-)
        self.max_torrents_active = 20
        self.max_torrents_per_source = 10
        self.source_interval = 100
        self.swarm_interval = 100

        # Can't be changed on runtime
        self.tracker_interval = 200
        self.logging_interval = 60
        self.share_mode_target = 3
        self.policy = policy(session)

        # Non-Configurable
        self.initial_logging_interval = 20
        self.initial_tracker_interval = 25
        self.initial_swarm_interval = 30
        self.min_connection_start = 5
        self.min_channels_start = 100
        self.credit_mining_path = os.path.join(DefaultDownloadStartupConfig.getInstance().get_dest_dir(),
                                               CREDIT_MINING_FOLDER_DOWNLOAD)
        self.load_config = load_config

        # whether we want to check dependencies of BoostingManager
        self.check_dependencies = True
        self.auto_start_source = True

        # in seconds
        self.time_check_interval = 2
        self.timeout_torrent_activity = 240

        # multiplier for aware downloading
        self.multiplier_credit_mining = 3

class BoostingManager(TaskManager):
    """
    Class to manage all the credit mining activities
    """

    MULTIPLIER_DL = [0, 0.2, 0.6, 1, 1.5, 3]
    DEFAULT_PRIORITY_TORRENT = 5

    def __init__(self, session, settings=None):
        super(BoostingManager, self).__init__()
        self._logger = logging.getLogger(self.__class__.__name__)

        logFormatter = logging.Formatter("%(asctime)s.%(msecs).03dZ-%(message)s", datefmt="%Y%m%dT%H%M%S")
        logFormatter.converter = time.gmtime
        fileHandler = logging.FileHandler("session_stat.log", mode="w")
        fileHandler.setFormatter(logFormatter)
        fileHandler.setLevel(logging.NOTSET)

        self.rootLogger = logging.getLogger("session_stat")
        self.rootLogger.addHandler(fileHandler)
        self.rootLogger.setLevel(logging.NOTSET)

        BoostingManager.__single = self
        self.boosting_sources = {}
        self.torrents = {}

        self.session = session

        self.finish_pre_dl = {}

        # use provided settings or a default one
        self.settings = settings or BoostingSettings(session, load_config=True)

        if self.settings.check_dependencies:
            assert self.session.get_libtorrent()
            assert self.session.get_torrent_checking()
            assert self.session.get_dispersy()
            assert self.session.get_torrent_store()
            assert self.session.get_enable_torrent_search()
            assert self.session.get_enable_channel_search()
            assert self.session.get_megacache()

        self.torrent_db = self.session.open_dbhandler(NTFY_TORRENTS)
        self.channelcast_db = self.session.open_dbhandler(NTFY_CHANNELCAST)

        if self.settings.load_config:
            self.load_config()

        if not os.path.exists(self.settings.credit_mining_path):
            os.makedirs(self.settings.credit_mining_path)

        self.pre_session = self.session.lm.ltmgr.create_session()
        # self.pre_session.set_settings(lt.high_performance_seed())
        ss = self.pre_session.get_settings()
        ss['disable_hash_checks'] = True
        ss['allow_reordered_disk_operations'] = True
        self.pre_session.set_settings(ss)

        hpc_settings = lt.high_performance_seed()
        changed_settings = ['request_timeout', 'active_limit', 'peer_timeout', 'max_allowed_in_request_queue',
                            'active_seeds', 'low_prio_disk', 'dht_upload_rate_limit', 'send_buffer_watermark',
                            'recv_socket_buffer_size', 'send_buffer_watermark_factor', 'allowed_fast_set_size'
                            'num_outgoing_ports', 'alert_queue_size', 'utp_dynamic_sock_buf', 'suggest_mode',
                            'send_socket_buffer_size', 'cache_expiry', 'active_tracker_limit', 'active_dht_limit',
                            'mixed_mode_algorithm', 'cache_buffer_chunk_size', 'file_pool_size', 'max_failcount',
                            'inactivity_timeout', 'connections_limit', 'send_buffer_low_watermark', 'max_rejects',
                            'max_queued_disk_bytes',  'read_job_every', 'unchoke_slots_limit', 'connection_speed',
                            'max_http_recv_buffer_size', 'max_out_request_queue', 'listen_queue_size', 'cache_size']

        ss = self.session.lm.ltmgr.get_session().get_settings()
        for c in changed_settings:
            if hasattr(hpc_settings, c):
                ss[c] = getattr(hpc_settings, c)

        ss['share_mode_target'] = self.settings.share_mode_target
        ss['upload_rate_limit'] = 0
        ss['seed_choking_algorithm'] = 1
        ss['num_outgoing_ports'] = 0
        ss['max_suggest_pieces'] = 20
        ss['connection_speed'] = 200
        ss['num_want'] = 100
        self.session.lm.ltmgr.get_session().set_settings(ss)

        self.session.add_observer(self.on_torrent_notify, NTFY_TORRENTS, [NTFY_UPDATE])

        self.register_task("CreditMining_select", LoopingCall(self._select_torrent),
                           self.settings.initial_swarm_interval, interval=self.settings.swarm_interval)

        self.register_task("CreditMining_scrape", LoopingCall(self.scrape_trackers),
                           self.settings.initial_tracker_interval, interval=self.settings.tracker_interval)

        self.register_task("CreditMining_log", LoopingCall(self.log_statistics),
                           self.settings.initial_logging_interval, interval=self.settings.logging_interval)

        self.register_task("CreditMining_checktime", LoopingCall(self.check_time),
                           self.settings.time_check_interval, interval=self.settings.time_check_interval)

        self.register_task("process resume", LoopingCall(self.__process_resume_alert), 10, interval=5)

        self.register_task("Sessionstats_log", LoopingCall(self.log_session_statistics),
                           self.settings.initial_logging_interval, interval=5)


    def shutdown(self):
        """
        Shutting down boosting manager. It also stops and remove all the sources.
        """
        self.save_config()
        self._logger.info("Shutting down boostingmanager")

        self.cancel_all_pending_tasks()

        for sourcekey in self.boosting_sources.keys():
            self.remove_source(sourcekey)

        # remove credit mining downloaded data
        shutil.rmtree(self.settings.credit_mining_path, ignore_errors=True)

        # remove pre-download file
        for f in glob.glob(self.session.get_downloads_pstate_dir()+"/_*.state"):
            os.remove(f)

    def get_source_object(self, sourcekey):
        """
        Get the actual object of the source key
        """
        return self.boosting_sources.get(sourcekey, None)

    def set_enable_mining(self, source, mining_bool=True, force_restart=False):
        """
        Dynamically enable/disable mining source.
        """
        for ihash in list(self.torrents):
            tor = self.torrents.get(ihash)
            if tor['source'] == source_to_string(source):
                self.torrents[ihash]['enabled'] = mining_bool

                # pause torrent download from disabled source
                if not mining_bool:
                    self.stop_download(ihash, reason="disabling source")

        self.boosting_sources[string_to_source(source)].enabled = mining_bool

        self._logger.info("Set mining source %s %s", source, mining_bool)

        if force_restart:
            self._select_torrent()

    def add_source(self, source):
        """
        add new source into the boosting manager
        """
        if source not in self.boosting_sources:
            args = (self.session, source, self.settings, self.on_torrent_insert)

            try:
                isdir = os.path.isdir(source)
            except TypeError:
                # this handle binary data that has null bytes '\00'
                isdir = False

            if isdir:
                self.boosting_sources[source] = DirectorySource(*args)
            elif source.startswith('http://') or source.startswith('https://'):
                self.boosting_sources[source] = RSSFeedSource(*args)
            elif len(source) == 20:
                self.boosting_sources[source] = ChannelSource(*args)
            else:
                self._logger.error("Cannot add unknown source %s", source)
                return

            if self.settings.auto_start_source:
                self.boosting_sources[source].start()

            self._logger.info("Added source %s", source_to_string(source))
        else:
            self._logger.info("Already have source %s", source_to_string(source))

    def remove_source(self, source_key):
        """
        remove source by stop the downloading and remove its metainfo for all its swarms
        """
        if source_key in self.boosting_sources:
            source = self.boosting_sources.pop(source_key)
            source.kill_tasks()
            self._logger.info("Removed source %s", source_to_string(source_key))

            rm_torrents = [torrent for _, torrent in self.torrents.items()
                           if torrent['source'] == source_to_string(source_key)]

            for torrent in rm_torrents:
                self.stop_download(torrent["metainfo"].get_infohash(), remove_torrent=True, reason="removing source")

            self._logger.info("Torrents download stopped and removed")

    def __insert_peer(self, infohash, ip, peer):
        peerlist = self.torrents[infohash]['peers']
        if ip not in peerlist.keys():
            self.torrents[infohash]['peers'][ip] = peer
        else:
            stored_peer = self.torrents[infohash]['peers'][ip]

            #TODO(ardhi) : compare stored peer data with new peer data here
            # Example :
            # if stored_peer['num_pieces'] != peer['num_pieces']:
            # if stored_peer['completed'] != peer['completed']:
            # if stored_peer['uinterested'] != peer['uinterested']:

            self.torrents[infohash]['peers'][ip] = peer

    def __process_resume_alert(self):
        _alerts = self.pre_session.pop_alerts() or []
        for a in _alerts:
            if a.category() & lt.alert.category_t.storage_notification and hasattr(a, 'resume_data'):
                basename = "_" + hexlify(a.resume_data['info-hash']) + '.state'
                filename = os.path.join(self.session.get_downloads_pstate_dir(), basename)

                with open(filename, 'wb') as file_:
                    file_.write(str(a.resume_data))

                # call the callback to start boosting on this torrent
                self.torrents[a.resume_data['info-hash']]['predownload'].callback(a.handle)

    def _pre_download_torrent(self, source, infohash, torrent, defer_obj=None):
        tdef = torrent['metainfo']
        metainfo = tdef.get_metainfo()
        torrentinfo = lt.torrent_info(metainfo)

        deferred_handle = defer_obj or defer.Deferred()

        if len(self.pre_session.get_torrents()) > 500:
            reactor.callLater(20, self._pre_download_torrent, source, infohash, torrent, defer_obj)
            return deferred_handle

        self._logger.info("%s start pre-downloading", hexlify(infohash))

        thandle = self.pre_session.add_torrent({'ti': torrentinfo, 'save_path': self.settings.credit_mining_path,
                                         'flags': lt.add_torrent_params_flags_t.flag_sequential_download |
                                                  lt.add_torrent_params_flags_t.flag_paused})

        # only download 4 pieces
        thandle.prioritize_pieces([0]*len(thandle.piece_priorities()))
        thandle.piece_priority(0, 7)
        thandle.piece_priority(1, 7)
        thandle.piece_priority(2, 7)
        thandle.piece_priority(3, 7)

        def _on_finish(_thandle):
            self.pre_session.remove_torrent(_thandle, 0)
            self.torrents[infohash]['predownload'] = "_" + hexlify(infohash) + '.state'

            out = ""
            for peer in self.torrents[infohash]['peers'].values():
                out += "torrent:%s\tip:%s\tuprate:%s\tdwnrate:%s\t#piece:%s\tprogress:%s\tpeak-up/down:%s/%s\tspeed:%d\tremote:%s/%s\twe:%s/%s\tsource:%d\trtt:%d\tcontype:%s\trecipro:%d++" \
                        %(hexlify(infohash), peer['ip'], peer['alluprate'], peer['alldownrate'], peer['num_pieces'], peer['completed'],
                          peer['uppeak'], peer['downpeak'], peer['speed'], peer['uinterested'], peer['uchoked'],
                          peer['dinterested'], peer['dchoked'], peer['source'], peer['rtt'], peer['connection_type'], peer['recipro'])

            num_seed, num_leech = utilities.translate_peers_into_health(self.torrents[infohash]['peers'].values())
            self.torrents[infohash]['num_seeders'] = self.torrents[infohash]['num_seeders'] or num_seed
            self.torrents[infohash]['num_leechers'] = self.torrents[infohash]['num_leechers'] or num_leech

            self._logger.debug("Seeder/leecher data %s translated from peers : seeder %s, leecher %s",
                                hexlify(infohash), num_seed, num_leech)

            self._logger.debug("peers %s : %s", hexlify(infohash), out or "None")
            return infohash


        deferred_handle.addCallback(_on_finish)
        deferred_handle.addErrback(log.err)

        self.finish_pre_dl[infohash] = 0.0

        def _check_swarm_peers(thandle, started_time):
            for p in thandle.get_peer_info():
                peer = LibtorrentDownloadImpl.create_peerlist_data(p)
                self.__insert_peer(infohash, peer['ip'], peer)

            status = thandle.status()
            elapsed_time = time.time() - started_time

            # maximal waiting time : after 3600 seconds (1 hour)
            if elapsed_time > 3600 and not self.finish_pre_dl[infohash]:
                self.cancel_pending_task("pre_download_%s" %hexlify(infohash))
                if status.progress < 1.0:
                    self._logger.warn("%s timeout pre-downloading with %f", hexlify(infohash), status.progress)

                thandle.pause()
                thandle.save_resume_data()

            # finished but waiting for more peers data for 10 minute
            if self.finish_pre_dl[infohash] and time.time() - self.finish_pre_dl[infohash] > 600:
                self.cancel_pending_task("pre_download_%s" %hexlify(infohash))
                thandle.pause()
                thandle.save_resume_data()

            # just finished, setting the flags
            if status.progress == 1.0 and not self.finish_pre_dl[infohash]:
                self._logger.info("%s finish pre-downloading by %s", hexlify(infohash), time.time() - started_time)
                self.finish_pre_dl[infohash] = time.time()

                # stop uploading
                thandle.set_max_uploads(0)

                tfile = thandle.torrent_file()

                # fill with fakes data to attract other peer
                # piece_num = len(thandle.piece_priorities())
                # midpiece = '0' * tfile.piece_size(0)
                # for p_idx in xrange(5, piece_num):
                #     thandle.add_piece(p_idx, midpiece if p_idx < piece_num-1 else '0' * tfile.piece_size(piece_num-1)
                #                       , 0)

        self.register_task("pre_download_%s" % hexlify(infohash), LoopingCall(_check_swarm_peers, thandle, time.time()), 0,  interval=2)
        thandle.resume()

        return deferred_handle

    def on_torrent_insert(self, source, infohash, torrent):
        """
        This function called when a source is finally determined. Fetch some torrents from it,
        then insert it into our data
        """

        # Remember where we got this torrent from
        # self._logger.debug("remember torrent %s from %s", torrent['name'], source_to_string(source))

        torrent['peers'] = {}

        if self.session.lm.load_download_pstate_noexc(infohash):
            torrent['predownload'] = "_" + hexlify(infohash) + '.state'
        else:
            torrent['predownload'] = self._pre_download_torrent(source, infohash, torrent)
            ##REPLACETHIS##

        torrent['source'] = source_to_string(source)

        boost_source = self.boosting_sources.get(source, None)
        if not boost_source:
            self._logger.info("Dropping torrent insert from removed source: %s", repr(torrent))
            return
        elif boost_source.archive:
            torrent['preload'] = True
            torrent['prio'] = 100

        # If duplicates exist, set is_duplicate to True, except for the one with the most seeders.
        duplicates = [other for other in self.torrents.values() if compare_torrents(torrent, other)]
        if duplicates:
            duplicates += [torrent]
            healthiest_torrent = max([(torrent['num_seeders'], torrent) for torrent in duplicates])[1]
            for duplicate in duplicates:
                is_duplicate = healthiest_torrent != duplicate
                duplicate['is_duplicate'] = is_duplicate
                if is_duplicate and duplicate.get('download', None):
                    self.stop_download(duplicate["metainfo"].get_infohash(), reason="duplicate")

        torrent['time'] = {}
        torrent['time']['all_download'] = 0
        torrent['time']['all_upload'] = 0
        torrent['time']['last_started'] = 0.0
        torrent['time']['last_stopped'] = 0.0
        torrent['time']['last_activity'] = 0.0
        torrent['time']['timeout'] = self.settings.timeout_torrent_activity

        torrent['availability'] = 0.0
        torrent['livepeers'] = []

        self.torrents[infohash] = torrent

    def on_torrent_notify(self, subject, change_type, infohash):
        """
        Notify us when we have new seeder/leecher value in torrent from tracker
        """
        if infohash not in self.torrents:
            return

        self._logger.debug("infohash %s %s %s updated", subject, change_type, hexlify(infohash))

        tdict = self.torrent_db.getTorrent(infohash, keys=['C.torrent_id', 'infohash', 'name',
                                                           'length', 'category', 'status', 'num_seeders',
                                                           'num_leechers'])

        if tdict:
            infohash_str = hexlify(tdict['infohash'])

            new_seed = tdict['num_seeders']
            new_leecher = tdict['num_leechers']

            if new_seed - self.torrents[tdict['infohash']]['num_seeders'] \
                    or new_leecher - self.torrents[tdict['infohash']]['num_leechers']:
                self.torrents[tdict['infohash']]['num_seeders'] = new_seed
                self.torrents[tdict['infohash']]['num_leechers'] = new_leecher
                self._logger.info("infohash %s : seeder/leecher changed seed:%d leech:%d",
                                  infohash_str, new_seed, new_leecher)

    def scrape_trackers(self):
        """
        Manually scrape tracker by requesting to tracker manager
        """

        for infohash in list(self.torrents):
            force_scrape = False

            # torrent handle
            lt_torrent = self.session.lm.ltmgr.get_session().find_torrent(lt.big_number(infohash))

            for i in lt_torrent.get_peer_info():
                peer = LibtorrentDownloadImpl.create_peerlist_data(i)

                # update peer information
                self.__insert_peer(infohash, peer['ip'], peer)

            num_seed, num_leech = utilities.translate_peers_into_health(self.torrents[infohash]['peers'].values())

            # calculate number of seeder and leecher by looking at the peers
            if self.torrents[infohash]['num_seeders'] == 0:
                self.torrents[infohash]['num_seeders'] = num_seed
                force_scrape = True
            if self.torrents[infohash]['num_leechers'] == 0:
                self.torrents[infohash]['num_leechers'] = num_leech
                force_scrape = True

            if force_scrape:
                self._logger.debug("Seeder/leecher data %s translated from peers : seeder %s, leecher %s",
                                   hexlify(infohash), num_seed, num_leech)

            # check health(seeder/leecher)
            self.session.lm.torrent_checker.add_gui_request(infohash, force_scrape)

    def set_archive(self, source, enable):
        """
        setting archive of a particular source. This affects all the torrents in this source
        """
        if source in self.boosting_sources:
            self.boosting_sources[source].archive = enable
            self._logger.info("Set archive mode for %s to %s", source, enable)
        else:
            self._logger.error("Could not set archive mode for unknown source %s", source)

    def __bdl_callback(self, ds):
        ihash_str = ds.get_download().tdef.get_infohash().encode('hex')

        peers = [x for x in ds.get_peerlist() if any(x['have']) and not
                 x['ip'].startswith("127.0.0")]

        ds.get_peerlist = lambda: peers

        availability = ds.get_availability()
        ihash = unhexlify(ihash_str)

        if ihash in self.torrents.keys():
            self.torrents[ihash]['availability'] = availability
            self.torrents[ihash]['livepeers'] = peers
            for peer in self.torrents[ihash]['livepeers']:
                self.__insert_peer(ihash, peer['ip'], peer)


        return 1.0, True

    def start_download(self, infohash):
        """
        Start downloading a particular torrent and add it to download list in Tribler
        """
        dscfg = DownloadStartupConfig()
        dscfg.set_dest_dir(self.settings.credit_mining_path)
        dscfg.set_safe_seeding(False)
        dscfg.dlconfig.set('downloadconfig', 'seeding_mode', 'forever')

        if not infohash:
            self._logger.error("None Infohash %s", infohash)
            return

        torrent = self.torrents[infohash]

        preload = torrent.get('preload', False)

        if self.session.lm.download_exists(torrent["metainfo"].get_infohash()):
            self._logger.error("Already downloading %s. Cancel start_download",
                               hexlify(torrent["metainfo"].get_infohash()))
            return

        pstate = None
        if type(torrent['predownload']) is not str:
            self._logger.error("Still predownload %s. Pending start_download %s",
                               hexlify(torrent["metainfo"].get_infohash()), torrent['predownload'])
            torrent['predownload'].addCallback(self.start_download)

            return
        elif os.path.isfile(os.path.join(self.session.get_downloads_pstate_dir(), torrent['predownload'])):
            with open(os.path.join(self.session.get_downloads_pstate_dir(), torrent['predownload']), 'r') as _predl_file:
                pstate_raw = _predl_file.read()

            pstate = dscfg.dlconfig.copy()
            if not pstate.has_section('state'):
                pstate.add_section('state')
            pstate.set('state', 'engineresumedata', pstate_raw)

            # as we read initial resume data, delete it afterwards
            os.remove(os.path.join(self.session.get_downloads_pstate_dir(), torrent['predownload']))

        self._logger.info("Starting %s preload %s",
                          hexlify(torrent["metainfo"].get_infohash()), preload)

        torrent['download'] = self.session.lm.add(torrent['metainfo'], dscfg, pstate=pstate, hidden=True,
                                                  share_mode=not preload, checkpoint_disabled=True)
        torrent['download'].set_priority(torrent.get('prio', self.DEFAULT_PRIORITY_TORRENT))
        torrent['download'].set_state_callback(self.__bdl_callback, True)

        torrent['time']['last_started'] = time.time()

        # assume last activity when start downloading
        torrent['time']['last_activity'] = time.time()

        # if it's paused
        if torrent['download'].handle:
            torrent['download'].handle.resume()

    def stop_download(self, infohash, remove_torrent=False, reason="N/A"):
        """
        Stopping torrent that currently downloading
        """
        torrent = self.torrents[infohash]
        infohash = hexlify(infohash)

        self._logger.info("Stopping %s, reason : %s", str(infohash), reason)
        download = torrent.get('download', None)
        if download:
            handle = download.handle
            if not handle.is_valid():
                self._logger.error("Handle %s is not valid", str(infohash))
            if not handle.has_metadata():
                self._logger.error("Metadata %s is not valid", str(infohash))
            handle.pause()

            self._logger.info("Writing resume data for %s", str(infohash))
            deferred_resume = download.save_resume_data()

            def _remove_download(resume_data, remove_torrent_par):
                infohash_bin = resume_data['info-hash']
                self._logger.info("[CALLBACK] Stopping download %s", hexlify(infohash_bin))

                if infohash_bin in self.torrents:
                    _torrent = self.torrents[infohash_bin]
                    _download = _torrent.pop('download', None)
                else:
                    self._logger.error("Can't find torrents in callback %s:%s", hexlify(infohash_bin),
                                       [hexlify(a) for a in self.torrents.keys()])
                    _download = None

                if _download:
                    self.session.remove_download(_download, hidden=True)
                    torrent['time']['last_stopped'] = time.time()
                if remove_torrent_par:
                    self.torrents.pop(infohash_bin)

            deferred_resume.addCallback(_remove_download, remove_torrent)

    def _select_torrent(self):
        """
        Function to select which torrent in the torrent list will be downloaded in the
        next iteration. It depends on the source and applied policy
        """
        torrents = {}
        for infohash in list(self.torrents):
            torrent = self.torrents.get(infohash)
            # we prioritize archive source
            if torrent.get('preload', False):
                if 'download' not in torrent:
                    self.start_download(infohash)
                elif torrent['download'].get_status() == DLSTATUS_SEEDING:
                    self.stop_download(infohash, reason="archive mode")
            elif not torrent.get('is_duplicate', False):
                if torrent.get('enabled', True):
                    torrents[infohash] = torrent

        if self.settings.policy is not None and torrents:
            # Determine which torrent to start and which to stop.
            torrents_start, torrents_stop = self.settings.policy.apply(
                torrents, self.settings.max_torrents_active)
            for torrent in torrents_stop:
                self.stop_download(torrent["metainfo"].get_infohash(), reason="by policy")
            for torrent in torrents_start:
                self.start_download(torrent["metainfo"].get_infohash())

            self._logger.info("Selecting from %s torrents %s start download", len(torrents), len(torrents_start))

    def load_config(self):
        """
        load config in file configuration and apply it to manager
        """
        self._logger.info("Loading config file from session configuration")

        def _add_sources(values):
            """
            adding sources in configuration file
            """
            for boosting_source in values:
                boosting_source = validate_source_string(boosting_source)
                self.add_source(boosting_source)

        def _archive_sources(values):
            """
            setting archive to sources
            """
            for archive_source in values:
                archive_source = validate_source_string(archive_source)
                self.set_archive(archive_source, True)

        def _set_enable_boosting(values, enabled):
            """
            set disable/enable source
            """
            for boosting_source in values:
                boosting_source = validate_source_string(boosting_source)
                if boosting_source not in self.boosting_sources.keys():
                    self.add_source(boosting_source)
                self.boosting_sources[boosting_source].enabled = enabled

        # set policy
        self.settings.policy = self.session.get_cm_policy(True)(self.session)

        for k in SAVED_ATTR:
            # see the session configuration
            object.__setattr__(self.settings, k, getattr(self.session, "get_cm_%s" %k)())

        for k, val in self.session.get_cm_sources().items():
            if k is "boosting_sources":
                _add_sources(val)
            elif k is "archive_sources":
                _archive_sources(val)
            elif k is "boosting_enabled":
                _set_enable_boosting(val, True)
            elif k is "boosting_disabled":
                _set_enable_boosting(val, False)

    def save_config(self):
        """
        save the environment parameters in config file
        """
        for k in SAVED_ATTR:
            try:
                setattr(self.session, "set_cm_%s" % k, getattr(self.settings, k))
            except OperationNotPossibleAtRuntimeException:
                # some of the attribute can't be changed in runtime. See lm.sessconfig_changed_callback
                self._logger.debug("Cannot set attribute %s. Not permitted in runtime", k)

        archive_sources = []
        lboosting_sources = []
        flag_enabled_sources = []
        flag_disabled_sources = []
        for boosting_source_name, boosting_source in \
                self.boosting_sources.iteritems():

            bsname = source_to_string(boosting_source_name)

            lboosting_sources.append(bsname)
            if boosting_source.enabled:
                flag_enabled_sources.append(bsname)
            else:
                flag_disabled_sources.append(bsname)

            if boosting_source.archive:
                archive_sources.append(bsname)

        self.session.set_cm_sources(lboosting_sources, CONFIG_KEY_SOURCELIST)
        self.session.set_cm_sources(flag_enabled_sources, CONFIG_KEY_ENABLEDLIST)
        self.session.set_cm_sources(flag_disabled_sources, CONFIG_KEY_DISABLEDLIST)
        self.session.set_cm_sources(archive_sources, CONFIG_KEY_ARCHIVELIST)

        self.session.save_pstate_sessconfig()

    def log_statistics(self):
        """Log transfer statistics"""
        lt_torrents = self.session.lm.ltmgr.get_session().get_torrents()

        for lt_torrent in lt_torrents:
            status = lt_torrent.status()

            infohash = unhexlify(str(status.info_hash))

            if infohash in self.torrents:
                self._logger.debug("Status for %s : %s %s | s/l : %d/%d, isdl:%s", status.info_hash,
                                   status.all_time_download, status.all_time_upload,
                                   self.torrents[infohash]['num_seeders'], self.torrents[infohash]['num_leechers'],
                                   self.torrents[infohash].get('download'))

                out = ""
                for peer in self.torrents[infohash]['peers'].values():
                    out += "torrent:%s\tip:%s\tuprate:%s\tdwnrate:%s\t#piece:%s\tprogress:%s\tpeak-up/down:%s/%s\tspeed:%d\tremote:%s/%s\twe:%s/%s\tsource:%d\trtt:%d\tcontype:%s\trecipro:%d++" \
                            %(hexlify(infohash), peer['ip'], peer['alluprate'], peer['alldownrate'], peer['num_pieces'], peer['completed'],
                              peer['uppeak'], peer['downpeak'], peer['speed'], peer['uinterested'], peer['uchoked'],
                              peer['dinterested'], peer['dchoked'], peer['source'], peer['rtt'], peer['connection_type'], peer['recipro'])

                self._logger.debug("peers %s : %s", hexlify(infohash), out or "None")
                # TODO(ardhi) : disable piece priorities call
                # piece_priorities will fail in libtorrent 1.0.9
                # if lt.__version__ == '1.0.9.0':
                #     continue
                # else:
                #     non_zero_values = []
                #     for piece_priority in lt_torrent.piece_priorities():
                #         if piece_priority != 0:
                #             non_zero_values.append(piece_priority)
                #     if non_zero_values:
                #         self._logger.debug("Non zero priorities for %s : %s", status.info_hash, non_zero_values)

    def check_time(self):
        """
        Function to check activity of a torrent
        :return:
        """
        for ihash in list(self.torrents):
            tor = self.torrents.get(ihash)

            # only consider active torrents
            if 'download' not in tor:
                continue

            if tor['download'].handle is None:
                return

            status = tor['download'].handle.status()

            # if it was paused before saving resume data
            if tor['download'].handle.is_paused():
                tor['download'].handle.resume()

            if status.all_time_download != tor['time']['all_download']\
                    or status.all_time_upload != tor['time']['all_upload']:
                self._logger.debug("Update last activity for %s : %s", hexlify(ihash), time.time())
                tor['time']['last_activity'] = time.time()

                tor['time']['all_download'] = status.all_time_download
                tor['time']['all_upload'] = status.all_time_upload

            self._logger.debug("Rate %s : %.1f kB/s dwn: %.1f kB/s up", status.info_hash, status.download_rate/1000,
                               status.upload_rate/1000)


        # check main download periodically
        num_dl, total_prio_tor = self._check_main_download(self.settings.multiplier_credit_mining)

        def _assign_priority(cm_priority):
            _lt_torrents = self.session.lm.ltmgr.get_session().get_torrents()

            for _lt_torrent in _lt_torrents:
                _status = _lt_torrent.status()
                # change priority (reduce load on main downloading)
                if cm_priority and int(cm_priority) != _status.priority:
                    _lt_torrent.set_priority(1)
                    # self._logger.info("Change priority %s from %d to %d", _status.info_hash,
                    #                   _status.get_priority(), cm_priority)

        # 1 is the lowest priority we'd want to assign
        new_prio = (total_prio_tor/float(num_dl) if num_dl else self.DEFAULT_PRIORITY_TORRENT) or 1
        _assign_priority(new_prio)

    def _check_main_download(self, idx_multiplier):

        total_cm_prio = sum([tr['download'].handle.status().priority or 0
                             for tr in self.torrents.values()
                             if tr and tr.get('download', False) and tr['download'].handle])

        total_main_prio = sum([dl_impl.handle.status().priority or 0
                               for dl_impl in self.session.lm.get_downloads()
                               if dl_impl and dl_impl.handle and
                               dl_impl.tdef.get_infohash() not in self.torrents])

        ret_cm_prio = total_cm_prio

        # self._logger.debug("Priority(Main/Total Current CM/Total New CM) : (%d/%d/%d)", total_main_prio, total_cm_prio,
        #                    ret_cm_prio)

        # if main downloading not that much, we can continue as it is
        # else, reduce priority
        if total_main_prio >= total_cm_prio:
            ret_cm_prio = total_cm_prio - self.MULTIPLIER_DL[idx_multiplier]*\
                                          (total_main_prio-total_cm_prio)

        return len([tr for tr in self.torrents.values() if tr and tr.get('download', False)]), ret_cm_prio

    def update_torrent_stats(self, torrent_infohash_str, seeding_stats):
        """
        function to update swarm statistics.

        This function called when we get new Downloadstate for active torrents.
        Updated downloadstate (seeding_stats) for a particular torrent is stored here.
        """
        if 'time_seeding' in self.torrents[torrent_infohash_str]['last_seeding_stats']:
            if seeding_stats['time_seeding'] >= self.torrents[torrent_infohash_str][
                    'last_seeding_stats']['time_seeding']:
                self.torrents[torrent_infohash_str]['last_seeding_stats'] = seeding_stats
        else:
            self.torrents[torrent_infohash_str]['last_seeding_stats'] = seeding_stats

    def log_session_statistics(self):
        """Log session statistics"""
        status = self.session.lm.ltmgr.get_session().status()

        self.rootLogger.error("SESSIONSTAT>%s:%s:%s:%s:%s:%s:%s:%s:%d",
                         status.total_download, status.total_upload,
                         status.total_payload_download, status.total_payload_upload,
                         status.total_dht_download, status.total_dht_upload,
                         status.total_ip_overhead_download, status.total_ip_overhead_upload,
                         status.num_unchoked)

