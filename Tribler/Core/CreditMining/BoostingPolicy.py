"""
Supported boosting policy.

Author(s): Egbert Bouman, Mihai Capota, Elric Milon, Ardhi Putra
"""
import logging
import random


class BoostingPolicy(object):
    """
    Base class for determining what swarm selection policy will be applied
    """

    def __init__(self, session):
        self.session = session
        # function that checks if key can be applied to torrent
        self.reverse = None

        self._logger = logging.getLogger(self.__class__.__name__)

    def apply(self, torrents, max_active):
        """
        apply the policy to the torrents stored
        """
        sorted_torrents = sorted([torrent for torrent in torrents.itervalues()
                                  if self.key_check(torrent)],
                                 key=self.key, reverse=self.reverse)

        torrents_start = []
        for torrent in sorted_torrents[:max_active]:
            if not self.session.get_download(torrent["metainfo"].get_infohash()):
                torrents_start.append(torrent)
        torrents_stop = []
        for torrent in sorted_torrents[max_active:]:
            if self.session.get_download(torrent["metainfo"].get_infohash()):
                torrents_stop.append(torrent)

        return torrents_start, torrents_stop

    def key(self, key):
        """
        function to find a key of an object
        """
        return None

    def key_check(self, key):
        """
        function to check whether a swarm is included to download
        """
        return False


class RandomPolicy(BoostingPolicy):
    """
    A credit mining policy that chooses a swarm randomly
    """
    def __init__(self, session):
        BoostingPolicy.__init__(self, session)
        self.reverse = False

    def key_check(self, key):
        return True

    def key(self, key):
        return random.random()


class CreationDatePolicy(BoostingPolicy):
    """
    A credit mining policy that chooses swarm by its creation date

    The idea is, older swarms need to be boosted.
    """
    def __init__(self, session):
        BoostingPolicy.__init__(self, session)
        self.reverse = True

    def key_check(self, key):
        return key['creation_date'] > 0

    def key(self, key):
        return key['creation_date']


class SeederRatioPolicy(BoostingPolicy):
    """
    Default policy. Find the most underseeded swarm to boost.
    """
    def __init__(self, session):
        BoostingPolicy.__init__(self, session)
        self.reverse = False

    def key(self, key):
        return key['num_seeders'] / float(key['num_seeders'] + key['num_leechers'])

    def key_check(self, key):
        return (key['num_seeders'] + key['num_leechers']) > 0
