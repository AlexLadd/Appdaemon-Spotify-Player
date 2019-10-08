"""
Appdaemon app to play Spotify songs on a Spotify connected device using an event fired from Home Assistant or Appdaemon.

Also provides the ability to control the Spotify player as well as take a snapshot and restore playback from the snapshot.

See https://github.com/AlexLadd/Appdaemon-Spotify-Player/blob/master/spotify_client.py for configuration and examples.
"""

import appdaemon.plugins.hass.hassapi as hass
import spotipy
import random
import datetime
import time
import voluptuous as vol
import requests
from bs4 import BeautifulSoup
import json
from pychromecast.controllers.spotify import SpotifyController
import pychromecast
from pychromecast.socket_client import (
    CONNECTION_STATUS_CONNECTED,
    CONNECTION_STATUS_DISCONNECTED,
    CONNECTION_STATUS_CONNECTING,
)

CONF_USERNAME = 'username'
CONF_PASSWORD = 'password'
CONF_DEBUGGING = 'debugging'
CONF_COUNTRY = 'country'
CONF_LANGUAGE = 'language'
CONF_USER_ALIASES = 'user_aliases'
CONF_DEVICE_ALIASES = 'device_aliases'
CONF_EVENT_DOMAIN_NAME = 'event_domain_name'

DEFAULT_EVENT_DOMAIN_NAME = 'spotify'
DEFAULT_EVENT_PLAY = '.play'
DEFAULT_EVENT_CONTROLS = '.controls'

DEFAULT_COUNTRY = 'CA'
DEFAULT_LANGUAGE = 'en_CA'

# Max number of times to retry playing a song
MAX_PLAY_ATTEMPTS = 2
# Max number of times to retry transfering a song
MAX_TRANSFER_ATTEMPTS = 2

def _is_spotify_country(value):
  """ ISO 3166-1 alpha-2 country code format (ex: 'US') """
  if value is None:
    raise vol.Invalid('country is None.')
  if not isinstance(value, str):
    raise vol.Invalid('country is not a string.')
  if len(value) != 2 or not value.isupper():
    raise vol.Invalid('Invalid country format, please use ISO 3166-1 alpha-2 country code format.')
  return value

def _is_spotify_language(value):
  """ ISO 639 language code and an ISO 3166-1 alpha-2 country code, joined by an underscore (ex: 'en_US') """
  if value is None:
    raise vol.Invalid('language is None.')
  if not isinstance(value, str):
    raise vol.Invalid('language is not a string.')
  if len(value) != 5 or not value[:2].islower() or value[2] != '_' or not _is_spotify_country(value[3:]):
    raise vol.Invalid('Invalid language format, please use an ISO 639 language code and an ISO 3166-1 alpha-2 country code, joined by an underscore.')
  return value

SPOTIFY_CLIENT_SCHEMA = vol.Schema(
  {
    vol.Required(CONF_USERNAME): str,                                               # Spotify username
    vol.Required(CONF_PASSWORD): str,                                               # Spotify password
    vol.Optional(CONF_EVENT_DOMAIN_NAME, default=DEFAULT_EVENT_DOMAIN_NAME): str,   # Change the default event domain name from 'spotify'
    vol.Optional(CONF_DEBUGGING, default=False): bool,                              # Adjust the verbosity of the logging output
    vol.Optional(CONF_COUNTRY, default=DEFAULT_COUNTRY): _is_spotify_country,       # Your country
    vol.Optional(CONF_LANGUAGE, default=DEFAULT_LANGUAGE): _is_spotify_language,    # Your language
    vol.Optional(CONF_USER_ALIASES, default={}): {str: str},                        # Map alias name to Spotify usernames
    vol.Optional(CONF_DEVICE_ALIASES, default={}): {str: str},                      # Map alias device name to Spotify device names
  }, 
  extra=vol.ALLOW_EXTRA
)


class SpotifyClient(hass.Hass):

  def initialize(self):
    config = SPOTIFY_CLIENT_SCHEMA(self.args)
    self._event_domain_name = config.get(CONF_EVENT_DOMAIN_NAME)
    self._event_play = self._event_domain_name + DEFAULT_EVENT_PLAY
    self._event_controls = self._event_domain_name + DEFAULT_EVENT_CONTROLS
    self._debugging = config.get(CONF_DEBUGGING)
    self._username = config.get(CONF_USERNAME)
    self._password = config.get(CONF_PASSWORD)
    self._country = config.get(CONF_COUNTRY)
    self._language = config.get(CONF_LANGUAGE)
    self._user_aliases = {}
    self._device_aliases = {}

    for alias, user in config.get('user_aliases').items():
      self._user_aliases[alias] = user
    for alias, device in config.get('device_aliases').items():
      self._device_aliases[alias] = device

    # AD logs will only show 'INFO' level messages using default settings
    if self._debugging:
      self.DEBUG_LEVEL = 'INFO'
    else:
      self.DEBUG_LEVEL = 'DEBUG'

    if self._event_domain_name != DEFAULT_EVENT_DOMAIN_NAME:
      self.log('Default event name has been changed to a custom event name: "{}".'.format(self._event_domain_name), level=self.DEBUG_LEVEL)

    self.sp = None                      # Spotify client object
    self._access_token = None           # Spotify access token
    self._token_expires = None          # Spotify token expiry in seconds
    self._chromecasts = {}              # Cast UUID -> CastDevice object
    self._spotify_devices = {}          # Spotify device_name -> device_id
    self._last_cast_sc = None           # The last SpotifyController used
    self._last_device = None            # The name of the Spotify device last used
    self._transfer_retry_count = 0      # Current number of song replay tries from cc error
    self._play_retry_count = 0          # Current number of song replay tries from spotify error
    self._snapshot_info = {}            # Captured snapshot information
    self._snapshot_uri = None           # Save last Spotify uri played (needed to restore from a list of tracks)

    # Register the Spotify play event listener
    self.listen_event(self._spotify_play_event_callback, event=self._event_play)

    # Register the Spotify controls event listener
    self.listen_event(self._spotify_controls_event_callback, event=self._event_controls)

    # Spotify web token is valid for 3600 seconds, so renew before 1 hour expires
    self.run_every(self._renew_spotify_token, self.datetime() + datetime.timedelta(seconds=2), 3500)


  def _renew_spotify_token(self, kwargs):
    """ Callback to renew spotify token """
    self._initialize_spotify_client()
    # Update the CC SpotifyController credentials when token is updated (Sometimes the music stops around the time the token expires)
    # If the last device used was a chromecast device and currently active than update its credentials
    # if self._last_device and self.is_active:
    #   cast = self._get_chromcast_device(self._last_device)
    #   if cast:
    #     self._update_cast_spotify_app_credentials()


  def _update_cast_spotify_app_credentials(self):
    """ 
    Update the Spotify app credentials on the cast SpotifyController that is currently active

    Currently experimental
    """
    if self._last_cast_sc:
      try:
        res = self._last_cast_sc.send_message({
          "type": "setCredentials",
          "credentials": self._access_token,
          "expiresIn": self._token_expires,
        })
        self.log('Updated cast SpotifyController app credentials.', level=self.DEBUG_LEVEL)
      except pychromecast.error.PyChromecastStopped as e:
        self.log('Error updating cast SpotifyController app credentials: {}'.format(e), level='WARNING')
    else:
      self.log('Tried to update cast_sc credentials but self._last_cast_sc does not exist.', level='WARNING')


  def _initialize_spotify_client(self):
    """ Refresh the Spotify client instance """
    access_token, expires = self._get_spotify_token(self._username, self._password)
    self._access_token = access_token
    self._token_expires = expires

    if not access_token:
      self.log('Did not retrieve access token information for Spotify.', level='WARNING')
    else:
      self.log('Spotify client successfully initialized.', level=self.DEBUG_LEVEL)
      self.sp = spotipy.Spotify(auth=access_token)


  def _get_spotify_token(self, username, password):
    """ 
    Starts session to get Spotify access token. (Modified version of spotify_token)
    This version logs in as a real web browser - more powerful token
    """
    # arbitrary cookies value and can be static
    cookies = {"__bon": "MHwwfC01ODc4MjExMzJ8LTI0Njg4NDg3NTQ0fDF8MXwxfDE="}
    user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_13_2) \
                  AppleWebKit/537.36 (KHTML, like Gecko) Chrome/63.0.3239.132 Safari/537.36"
    headers = {'user-agent': user_agent}
    
    session = requests.Session()
    response = session.get("https://accounts.spotify.com/login", headers=headers, cookies=cookies)
    response.raise_for_status()
    csrf_token = response.cookies['csrf_token']

    data = {"remember": False, "username": username, "password": password, "csrf_token": csrf_token}
    response = session.post("https://accounts.spotify.com/api/login", data=data, cookies=cookies, headers=headers)
    response.raise_for_status()

    response = session.get("https://open.spotify.com/browse", headers=headers, cookies=cookies)
    response.raise_for_status()
    data = response.content.decode("utf-8")

    xml_tree = BeautifulSoup(data, 'lxml')
    script_node = xml_tree.find("script", id="config")
    config = json.loads(script_node.string)
    
    access_token = config['accessToken']
    expires_timestamp = config['accessTokenExpirationTimestampMs']
    expires_in = int(expires_timestamp) // 1000 - int(time.time())

    return (access_token, expires_in)


  def map_chromecasts(self, device):
    """ 
    Map device to Chromecast device name 
    
    param device: Spotify device id/media_player entity_id/Alias (from app config device_aliases)
    """
    if device in self._device_aliases:
      return self._device_aliases[device]
    cc_name = self.map_entity_to_chromecast(device)
    if cc_name:
      return cc_name
    dev_name = self._map_spotify_devid_to_name(device)
    if dev_name:
      return dev_name
    return device


  def map_chromecast_to_entity(self, name):
    """ Map chromecast names to media_player entity id """
    for mp in self.get_state("media_player").values():
      if mp['attributes']['friendly_name'] == name:
        return mp['entity_id']
    return None


  def map_entity_to_chromecast(self, entity_id):
    """ Map entity id to chromecast name """
    for mp in self.get_state("media_player").values():
      if mp['entity_id'] == entity_id:
        return mp['attributes']['friendly_name']
    return None


  def _map_spotify_usernames(self, name):
    """ Map alias name to spotify usernames """
    if name in self._user_aliases:
      return self._user_aliases[name]
    return name


  def _map_spotify_devid_to_name(self, dev_id):
    """ Map Spotify device id to device name using cached Spotify devices """
    return next((name for name, id in self._spotify_devices.items() if id == dev_id), None)


  def get_spotify_uri_type(self, uri):
    """ Returns the type of the Spotify uri (ex: artist, playlist, track, album, etc) """
    parts = uri.split(':')
    if len(parts) < 3:
      self.log('Invalid Spotify uri: {}.'.format(uri), level='WARNING')
      return ''
    return parts[-2]


  def is_spotify_uri(self, uri, media_type=None):
    """ 
    Verify if the uri is a valid Spotify uri

    param uri: Spotify uri (Format: spotify:(track|playlist|artist|album):twenty-two-digits-here)
    param media_type: The uri type ('track', 'playlist', 'artist', 'album') to check for
    """
    if not uri or not isinstance(uri, str):
      return False

    if isinstance(media_type, str):
      media_type = [media_type]
    elif not media_type:
      media_type = ['track', 'playlist', 'artist', 'album']

    parts = uri.split(':')
    if len(parts) == 3 and parts[0] == 'spotify' and parts[1] in media_type and len(parts[2]) == 22:
      return True
    return False

  def is_artist_uri(self, uri):
    """ Test if the given uri is a valid Spotify artist uri """
    return self.is_spotify_uri(uri, 'artist')

  def is_track_uri(self, uri):
    """ Test if the given uri is a valid Spotify track uri """
    return self.is_spotify_uri(uri, 'track')

  def is_playlist_uri(self, uri):
    """ Test if the given uri is a valid Spotify playlist uri """
    return self.is_spotify_uri(uri, 'playlist')

  def is_album_uri(self, uri):
    """ Test if the given uri is a valid Spotify album uri """
    return self.is_spotify_uri(uri, 'album')


  ######################   PLAY SPOTIFY MUSIC METHODS   ########################

  def transfer_playback_timer_callback(self, kwargs):
    """ Callback for scheduler calls to call transfer_playback """
    self.transfer_playback(kwargs['device'], kwargs.get('force_cc_update', False))


  def transfer_playback(self, device, force_cc_update=False):
    """ 
    Transfer Spotify music to another device - Top level call

    param device: Spotify device name/media_player id/Spotify device id
    param force_cc_update: Force a chromecast update
    """
    device_name = self.map_chromecasts(device)

    dev_id = self._get_spotify_device_devid(device_name, force_cc_update)

    success = False
    if dev_id:
      self._last_device = device_name
      success = self._transfer_playback(dev_id, True)
      
    # No Spotify device was found or playback wasn't transfered correctly, retry if below limit
    if not success or dev_id is None: 
      if self._transfer_retry_count < MAX_TRANSFER_ATTEMPTS:
        self.log('Retrying transfering playback now...', level=self.DEBUG_LEVEL)
        self._transfer_retry_count += 1
        self.run_in(self.transfer_playback_timer_callback, 2, device=device, force_cc_update=True)
        return
      else:
        self.log('Max retries reached trying to transfer playback to: "{}".'.format(device_name), level='ERROR')

    self._transfer_retry_count = 0


  def _transfer_playback(self, spotify_device_id, force_play=True):
    """ 
    Transfer Spotify music to another device

    param device: Valid Spotify device id
    param force_play: State of playback when transfered (True: Play, False: Maintain current state)
    """
    device_name = self._map_spotify_devid_to_name(spotify_device_id) or spotify_device_id
    try:
      self.sp.transfer_playback(device_id=spotify_device_id, force_play=force_play)
      self.log('Transfering music to: "{}".'.format(device_name), level=self.DEBUG_LEVEL)
    except spotipy.client.SpotifyException as e:
      # This can occur when a cached device is used that has been reconnected/dropped/disconnected from Spotify
      self.log('Error transfering music on Spotify device ("{}"): {}'.format(device_name, e), level='ERROR')
      return False
    return True


  def play_timer_callback(self, kwargs):
    """ Callback for scheduler calls to call play """
    self.play(kwargs['device'], kwargs['uri'], kwargs.get('off_set', None), kwargs.get('force_cc_update', False))


  def play(self, device, uri, offset=None, force_cc_update=False):
    """ 
    Top level call to play Spotify song

    param device: Spotify device name/media_player id/Spotify device id
    param uri: Spotify track/playlist/artist/album uri/list of tracks
    param offset: Provide offset as an int or track uri to start playback at a particular offset.
    param force_cc_update: Force a chromecast update
    """
    device_name = self.map_chromecasts(device)

    # Only a list of tracks can be played
    if isinstance(uri, list):
      for u in uri:
        if not self.is_track_uri(u):
          self.log("Invalid list of Spotify uri's, the song will not play. Only a list of tracks can be played.".format(uri), level='WARNING')
          return
    elif not self.is_spotify_uri(uri):
      self.log('Invalid Spotify uri: "{}", the song will not play.'.format(uri), level='WARNING')
      return

    dev_id = self._get_spotify_device_devid(device_name, force_cc_update)

    success = False
    if dev_id:
      self._last_device = device_name
      success = self._play(dev_id, uri, offset)

    if not success or dev_id is None:
      if self._play_retry_count < MAX_PLAY_ATTEMPTS:
        self.log('Retrying playing Spotify music now...', level=self.DEBUG_LEVEL)
        self._play_retry_count += 1
        self.run_in(self.play_timer_callback, 1, device=device_name, uri=uri, off_set=offset, force_cc_update=True)
        return
      else:
        self.log('Max retries reached trying to play Spotify music on: "{}". No music will play.'.format(device_name), level='ERROR')

    self._play_retry_count = 0


  def _play(self, spotify_device_id, uri, offset=None):
    """ 
    Play music on Spotify device using valid spotify uri (track, playlist, artist, album) and device id 

    param spotify_device_id: Spotify device id
    param uri: A valid Spotify uri
    param offset: Provide offset as an int or track uri to start playback at a particular position. (Works for playlist/album/list of tracks)
    """
    # Offset format: {“position”: <int>} or {“uri”: “<track uri>”}
    if isinstance(offset, int):
      o = { 'position' : offset }
    elif isinstance(offset, str):
      o = { 'uri' : offset }
    else:
      o = offset

    device_name = self._map_spotify_devid_to_name(spotify_device_id) or spotify_device_id
    try:
      if isinstance(uri, str) and uri.find('track') > 0:
        self.sp.start_playback(device_id=spotify_device_id, uris=[uri], offset=o)
      elif isinstance(uri, list) and uri[0].find('track') > 0:
        self.sp.start_playback(device_id=spotify_device_id, uris=uri, offset=o)
      else:
        self.sp.start_playback(device_id=spotify_device_id, context_uri=uri, offset=o)
      # Save last played uri for potentially restoring list of tracks playback later
      self._snapshot_uri = uri
      # Log the appropriate messages based on uri type
      self._log_playback_action(uri, device_name)
    except spotipy.client.SpotifyException as e:
      # This can occur when a cached device is used that has been reconnected/dropped/disconnected from Spotify
      self.log('Error playing music on Spotify device ("{}"): {}'.format(device_name, e), level='ERROR')
      return False
    return True


  def _get_spotify_device_devid(self, device_name, force_cc_update=False):
    """
    Get Spotify device id from the device name

    This may require connecting Spotify app to a Chromecast device

    param device_name: Spotify device name
    """
    # Check if Spotify is already connected to the device if device is not a CC or no CC update required
    dev_id = None
    is_cc_device = self._get_chromcast_device(device_name) is not None
    if not is_cc_device:
      self._last_cast_sc = None
    if not force_cc_update and (not is_cc_device or (is_cc_device and self._last_cast_sc)):
      dev_id = self._search_spotify_for_device(device_name)

    # We don't already have the device, look for a chromecast
    if dev_id is None and is_cc_device:
      if not self._register_spotify_on_cast_device(device_name):
        # Failed to connect Spotify to Chromecast
        return None

      # Look for our device again
      dev_id = self._search_spotify_for_device(device_name)

    return dev_id


  def _search_spotify_for_device(self, device_name):
    """ 
    Returns the Spotify device id given the name

    param device_name: The Spotify device name
    """
    # Use cached Spotify device if possible
    if device_name in self._spotify_devices:
      self.log('Cached Spotify device used.', level=self.DEBUG_LEVEL)
      return self._spotify_devices[device_name]
    else:
      devs = self.sp.devices()
      for d in devs['devices']:
        if d['name'] == device_name:
          self.log('Newly discovered Spotify device used.', level=self.DEBUG_LEVEL)
          self._spotify_devices[device_name] = d['id']
          return d['id']

    return None


  def _get_chromcast_device(self, device_name):
    """
    Returns the chromecast device object that matches the device_name
    Uses CastDevice class to listen to the cast connection and let us know when an update is needed

    param device_name: The chromecast device name
    """
    for cast in self._chromecasts.values():
      if device_name == cast.name:
        if cast.available:
          self.log('Cached chromecast device used.', level=self.DEBUG_LEVEL)
          return cast.get_cast()
        else:
          # Attempt to reconnect the unavailable cast
          self.log('Attempting to reset cast connection for: {}'.format(cast.name), level=self.DEBUG_LEVEL)
          cast.reset_cast_connection()
          if cast.available:
            return cast.get_cast()

    # We have not discovered the cast yet or the reconnection attempt failed
    chromecasts = pychromecast.get_chromecasts(tries=5, retry_wait=1, timeout=30)

    _cast = None
    for cast in chromecasts:
      if cast.name == device_name:
          _cast = cast

      if cast.uuid not in self._chromecasts:
        self.log('Found a new Chromecast device: {}'.format(cast.name), level=self.DEBUG_LEVEL)
        c = CastDevice(cast, self, self.DEBUG_LEVEL)
        self._chromecasts[c.uuid] = c
      else:
        # Try to update an existing CastDevice that is disconnected
        if not self._chromecasts[cast.uuid].available:
          self.log('Updated existing CastDevice: {}'.format(self._chromecasts[cast.uuid].name), level=self.DEBUG_LEVEL)
          self._chromecasts[cast.uuid].set_cast(cast)

    return _cast


  def _register_spotify_on_cast_device(self, cast_name):
    """ 
    Register Spotify app on a given chromecast device 
    
    param cast_name: Chromecast device name
    """
    # Get the chromecast object
    cast = self._get_chromcast_device(cast_name)
    if not cast:
      self.log('No chromecast device was found with the name: "{}".'.format(cast_name), level='WARNING')
      return False
    try:
      cast.wait(timeout=3)
    except RuntimeError as e:
      # We are already connected? Or we were connected and the thread died? Or no success connecting at all?
      self.log('Chromecast threading error while waiting for "{}": {}.'.format(cast_name, e), level='ERROR')
      return False 

    cast_sc = SpotifyController(self._access_token, self._token_expires)
    self._last_cast_sc = cast_sc
    cast.register_handler(cast_sc)
    try:
      cast_sc.launch_app(timeout=10)
    except pychromecast.error.LaunchError as e:
      self.log('Chromecast error waiting for status response from Spotify on "{}": "{}".'.format(cast_name, e), level='ERROR')
      return False
    except pychromecast.error.NotConnected as e:
      self.log('Chromecast connection failed on "{}": {}.'.format(cast_name, e), level='ERROR')
      return False
    except pychromecast.error.PyChromecastStopped as e:
      self.log('Chromecast threading error while launching app on "{}": {}.'.format(cast_name, e), level='ERROR')
      return False

    # Make sure everything was initialized correctly
    if not cast_sc.is_launched and not cast_sc.credential_error:
      self.log('Failed to launch spotify controller due to timeout.', level='ERROR')
      return False
    if not cast_sc.is_launched and cast_sc.credential_error:
      self.log('Failed to launch spotify controller due to credentials error.', level='ERROR')
      return False

    return True


  def _log_playback_action(self, uri, device):
    """ 
    Log Spotify playback action based on uri and device - For debugging purposes 
    
    param uri: A valid Spotify uri
    param device: Device name music is playing on
    """
    if isinstance(uri, str):
      if self.is_track_uri(uri):
        track = self.get_track_info(uri)
        self.log('Playing: "{}" by "{}" on "{}" speaker.'.format(track['name'], track['artist'], device), level=self.DEBUG_LEVEL)
      elif self.is_playlist_uri(uri):
        pl = self.get_playlist_info(uri)
        self.log('Playing playlist named: "{}" on "{}" speaker.'.format(pl['name'], device), level=self.DEBUG_LEVEL)
      elif self.is_artist_uri(uri):
        artist = self.get_artist_info(uri)
        self.log('Playing music from artist: "{}" on "{}" speaker.'.format(artist['name'], device), level=self.DEBUG_LEVEL)
      elif self.is_album_uri(uri):
        album = self.get_album_info(uri)
        self.log('Playing music from the album: "{}" by "{}" on "{}" speaker.'.format(album['name'], album['artist'], device), level=self.DEBUG_LEVEL)
      else:
        self.log('Playing something unknown: "{}" on "{}" speaker.'.format(uri, device), level=self.DEBUG_LEVEL)
    else:
      track = self.get_track_info(uri[0])
      self.log('Playing "{}" tracks starting with "{}" by "{}" on "{}" speaker.'.format(len(uri), track['name'], track['artist'], device), level=self.DEBUG_LEVEL)

  ######################   PLAY SPOTIFY MUSIC METHODS END   ########################


  ######################   SPOTIFY DEVICE CONTROLS METHODS   ########################

  def _spotify_controls_event_callback(self, event_name, data, kwargs):
    """
    Callback for the controls event used to control the active Spotify device from HA or AD

    Event Data Parameters:

    Actions -> pause, stop, resume, next, previous, set_volume, 
    increase_volume, decrease_volume, mute, snapshot, restore

    device: The device to restore the playback on (optional)

    volume_level: Desired volume level (0 - 100)

    transfer_playback: The device name to transfer the music to

    """
    action = data.get('action', None)

    if action == 'pause':
      self.log('Spotify device paused.', level=self.DEBUG_LEVEL)
      self.pause()
    elif action == 'resume':
      self.log('Spotify device resumed.', level=self.DEBUG_LEVEL)
      self.resume()
    elif action == 'stop':
      self.log('Spotify device stopped.', level=self.DEBUG_LEVEL)
      self.pause()
    elif action in ['skip', 'next', 'next_track']:
      self.log('Spotify device skipped track.', level=self.DEBUG_LEVEL)
      self.next_track()
    elif action in ['previous', 'previous_track']:
      self.log('Spotify device skipped to previous track.', level=self.DEBUG_LEVEL)
      self.previous_track()
    elif action == 'decrease_volume':
      self.log('Reduced Spotify device volume.', level=self.DEBUG_LEVEL)
      current_volume = self.current_volume
      self.set_volume(current_volume - 5)
    elif action == 'increase_volume':
      self.log('Increased Spotify device volume.', level=self.DEBUG_LEVEL)
      current_volume = self.current_volume
      if current_volume < 0:
        current_volume = 0
      self.set_volume(current_volume + 5)
    elif action == 'mute':
      self.log('Spotify device was muted.', level=self.DEBUG_LEVEL)
      self.set_volume(0)
    elif action == 'snapshot':
      self.log('Taking a snapshot of the current playback.', level=self.DEBUG_LEVEL)
      self.take_playback_snapshot()
    elif action == 'restore':
      self.log('Resuming playback from the previous snapshot.', level=self.DEBUG_LEVEL)
      device = data.get('device', None)
      self.restore_playback_from_snapshot(device)

    if 'volume_level' in data:
      volume_level = data.get('volume_level', None)
      if volume_level:
        try:
          self.set_volume(int(volume_level))
          self.log('Set the current Spotify device volume to "{}" percent.'.format(volume_level), level=self.DEBUG_LEVEL)
        except ValueError:
          self.log('Please specify a volume_level between 0 and 100 to set the Spotify device volume.', level='WARNING')

    if 'transfer_playback' in data:
      device = data.get('transfer_playback', None)
      self.transfer_playback(device)


  @property
  def is_active(self):
    """ Returns if Spotify is currently connected to a device """
    return self.sp.current_playback() is not None


  def get_active_device(self):
    """ Get the active Spotify speaker name """
    playback_info = self.sp.current_playback()
    if playback_info:
      return playback_info['device']['name']
    return None


  def repeat(self, state, device=None):
    """
    Sets the Spotify device's repeat state

    param state: Desired repeat state (track, context, or off)
    param device: Spotify device id (or name if the play method has cached the device)
      -> None will set repeat on the current device
    """
    if self.is_active:
      device_id = self.map_chromecasts(device)
      if device_id in self._spotify_devices:
        device_id = self._spotify_devices[device_id]
      self.sp.repeat(state, device_id)


  def repeat_state(self):
    """  Return the repeat state for the current device """
    if self.is_active:
      return self.sp.current_playback().get('repeat_state')
    return None
  

  def shuffle(self, state, device=None):
    """
    Sets the Spotify device's shuffle state

    param state: Desired shuffle state (True/False)
    param device: Spotify device id (or name if the play method has cached the device) (optional)
      -> None will set shuffle on the current device
    """
    if self.is_active:
      device_id = self.map_chromecasts(device)
      if device_id in self._spotify_devices:
        device_id = self._spotify_devices[device_id]
      self.sp.shuffle(state, device_id)


  def shuffle_state(self):
    """ Return the shuffle state for the current device """
    if self.is_active:
      return self.sp.current_playback().get('shuffle_state')
    return None


  def next_track(self):
    """ Skip to the next track """
    if self.is_active:
      self.sp.next_track()


  def previous_track(self):
    """ Skip to previous track """
    if self.is_active:
      self.sp.previous_track()


  def pause(self):
    """ Pause the playback """
    if self.is_active:
      self.sp.pause_playback()


  def resume(self):
    """ Resume the playback """
    if self.is_active:
      self.sp.start_playback()


  @property
  def current_volume(self):
    """ Returns the current active device volume level in percent """
    if self.is_active:
      return self.sp.current_playback().get('device', {}).get('volume_percent', None)
    else:
      return 0


  def set_volume(self, volume):
    """ 
    Set the volume level on the currently device
    
    param volume: Desired volume level (0 - 1)
    """
    if self.is_active:
      if 1 < volume < 100:
        volume = volume / 100
      self.sp.volume(int(volume*100))


  def seek_track(self, position_ms, device=None):
    """ 
    Seek to position in current track

    param position_ms: Desired track position in milliseconds
    param device: Spotify device id (or name if the play method has cached the device)
      -> None will set seek position in the current device
    """
    if self.is_active:
      device_id = self.map_chromecasts(device)
      if device_id in self._spotify_devices:
        device_id = self._spotify_devices[device_id]
      self.sp.seek_track(position_ms, device_id)


  ######################   SPOTIFY DEVICE CONTROLS METHODS END   ########################


  ######################   PLAYBACK SNAPSHOT METHODS   ########################

  def take_playback_snapshot(self):
    """ 
    Take snapshot to allow us to resume playback later with this information
    """
    # Reset previous snapshot info
    self._snapshot_info = {}

    result = self.sp.current_playback()
    if not result: # Nothing is currently playing
      self.log('Nothing is currently playling.', level='INFO')
      return

    self._snapshot_info['device_id'] = result['device']['id']
    self._snapshot_info['device_name'] = result['device']['name']
    self._snapshot_info['shuffle_state'] = result['shuffle_state']
    self._snapshot_info['repeat_state'] = result['repeat_state']
    self._snapshot_info['currently_playing_type'] = result['currently_playing_type']
    self._snapshot_info['currently_playing_uri'] = result.get('item', {}).get('uri', 'Could not find uri')
    if result['context']:
      self._snapshot_info['context'] = result.get('context', {}).get('uri', False)
    self._snapshot_info['progress_ms'] = result['progress_ms']

    self.log('Snapshot taken from: "{}".'.format(self._snapshot_info['device_name']), level=self.DEBUG_LEVEL)
    # self.pause()

  
  def restore_playback_from_snapshot(self, device=None):
    """ 
    Resume playback with the info from the previous snapshot

    param device: Spotify device name to restore the playback on (optional)
    """
    if not self._snapshot_info:
      self.log('Cannot restore playback since the previous snapshot did not capture anything.', level='WARNING')
      return

    if self._snapshot_info.get('context', False): 
      # A playlist, album, artist was previously playing
      uri = self._snapshot_info['context']
      offset = self._snapshot_info['currently_playing_uri']
    else: 
      if isinstance(self._snapshot_uri, list):
        # A list of tracks was previously playing
        uri = self._snapshot_uri
        offset = self._snapshot_info['currently_playing_uri']
      else:
        # A single track was previously playing
        uri = self._snapshot_info['currently_playing_uri']
        offset = None

    dev = device if device else self._snapshot_info['device_name']

    self.log('Restoring snapshot to: "{}".'.format(self.map_chromecasts(dev)), level=self.DEBUG_LEVEL)

    # Resume playing at the track we left off at
    self.play(dev, uri, offset)
    # Skip to the last position in the previously playing track
    self.seek_track(self._snapshot_info['progress_ms'])
    self.run_in(self._restore_seek, 0.5, progress_ms=self._snapshot_info['progress_ms'])


  def _restore_seek(self, kwargs):
    self.seek_track(kwargs['progress_ms'])

  ######################   PLAYBACK SNAPSHOT METHODS END   ########################


  ######################   MUSIC RECOMMENDATION METHODS   ########################

  def get_spotify_recommendation(self, artists=None, genres=None, tracks=None, limit=10):
    """
    Returns recommended tracks as a list or uri's
    This method will recommend tracks from various artists

    param artist: Spotify artist uris or names
    param genres: Genres of music (ex: rock)
    param tracks: Spotify track uris or names
    param limit: Limit of tracks to return (1-100)
    """
    if not any([artists, genres, tracks]):
      self.log('Please specify one or more of artists, genres, or tracks.', level='WARNING')
      return []

    if isinstance(tracks, str):
      if not self.is_track_uri(tracks): # assumes track name was passed in
        tracks = self.get_track_info(tracks).get('uri', tracks)
      tracks = [tracks]
    if isinstance(genres, str):
      genres = [genres]
    if isinstance(artists, str):
      if not self.is_artist_uri(artists): # assumes artist name was passed in
        artists = self.get_artist_info(artists).get('uri', artists)
      artists = [artists]

    results = self.sp.recommendations(seed_artists=artists, seed_genres=genres, seed_tracks=tracks, limit=limit, min_popularity=50)
    return [u['uri'] for u in results['tracks']]


  def get_recommendation_genre_seeds(self):
    """
    Returns the available genres for the get_spotify_recommendation() method as a list of genre strings
    """
    return self.sp.recommendation_genre_seeds().get('genres', [])


  def new_releases(self, country=None, limit=20, offset=0):
    """
    Returns new album releases on Spotify as a list of album uri's

    param country: Valid ISO 3166-1 alpha-2 country code
    param limit: The number of categories to return
    param offset: The index of the first item to return
    """
    results = self.sp.new_releases(country=(country or self._country), limit=20, offset=0)
    return [u['uri'] for u in results['albums']['items']]


  def get_playlists_by_category(self, category, country=None, limit=10, offset=0):
    """
    Returns new playlist releases featured on Spotify as a list or playlist uri's
    
    param category: A valid Spotify category (can be found from get_categories method)
    param country: Valid ISO 3166-1 alpha-2 country code
    param limit: The number of desired albums
    param offset: The index of the first item to return
    """
    categories = self.get_categories(country=(country or self._country), limit=50) # Limit of 50 should retrieve all categories possible
    if category not in categories:
      self.log('Invalid category: "{}", valid categories are: {}.'.format(category, categories), level='WARNING')
      return []

    result = self.sp.category_playlists(category, country=(country or self._country), limit=limit, offset=offset)
    return [u['uri'] for u in result['playlists']['items']]


  def get_categories(self, country=None, locale=None, limit=10, offset=0):
    """
    Returns valid category id's as a list or strings

    param country: Valid ISO 3166-1 alpha-2 country code
    param locale: Desired language (ISO 639 language code and an ISO 3166-1 alpha-2 country code, joined by an underscore)
    param limit: The number of categories to return
    param offset: The index of the first item to return
    """
    result = self.sp.categories(country=(country or self._country), locale=(locale or self._language), limit=limit, offset=offset)
    return [i['id'] for i in result['categories']['items']]


  def get_top_tracks(self, artist, country=None):
    """
    Returns the top 10 tracks from an artist as a list or uri's
    This method will recommend tracks from the specified artist

    param artist: Spotify artist uri or name
    param country: Valid ISO 3166-1 alpha-2 country code
    """
    if not self.is_artist_uri(artist):
      artist = self.get_artist_info(artist).get('uri', artist)

    if not self.is_artist_uri(artist):
      self.log('Invalid artist: {}.'.format(artist))
      return []

    results = self.sp.artist_top_tracks(artist, country=(country or self._country))
    return [u['uri'] for u in results['tracks']]


  def get_featured_playlists(self, country=None, locale=None, limit=10):
    """
    Returns featured playlists as a list of uri's

    param country: Valid ISO 3166-1 alpha-2 country code
    param locale: Desired language (ISO 639 language code and an ISO 3166-1 alpha-2 country code, joined by an underscore)
    param limit: The number of playlists to return
    """
    res = self.sp.featured_playlists(locale=(locale or self._language), country=(country or self._country), timestamp=datetime.datetime.now().isoformat(), limit=limit)
    return [u['uri'] for u in res['playlists']['items']]


  def get_artist_tracks(self, artist, limit=10, similar_artists=False, random_search=False):
    """
    Returns artist tracks as a list of uri's

    param artist: Spotify artist uri or name
    param limit: Limit of tracks to find
    param similar_artists: Find tracks from similar artists
    param random_search: Randomize the search results
    """
    if not self.is_artist_uri(artist):
      search_artist = self.get_artist_info(artist).get('uri', artist)
    else:
      search_artist = artist

    if not search_artist:
      return []

    res = []
    if not similar_artists:
      # Find tracks from provided artists
      tracks = self.get_top_tracks(search_artist)
      if tracks:
        res = tracks
      if len(res) < limit:
        artist_albums = self.get_artist_albums(search_artist)
        if artist_albums:
          if random_search:
            random.shuffle(artist_albums)
          for album in artist_albums:
            if len(res) >= limit:
              break
            tracks = self.get_album_tracks(album)
            res += tracks
    if not res:
      # Find tracks from similar artists
      if len(res) < limit:
        related_artists = self.get_related_artists(search_artist)
        if related_artists:
          if random_search:
            random.shuffle(related_artists)
          for artist in related_artists:
            if len(res) >= limit:
              break
            tracks = self.get_top_tracks(artist)
            if tracks:
              res += tracks

    if len(res) < limit:
      res += self.get_spotify_recommendation(artists=search_artist, limit=limit)
    if random_search:
      random.shuffle(res)
    return res[:limit]

  ######################   MUSIC RECOMMENDATION METHODS END   ########################


  ######################   MUSIC RECOMMENDATION HELPER METHODS   ########################

  def get_related_artists(self, artist):
    """
    Returns artists related to the given artist as a list of uri's 

    param artist: Spotify artist uri or name
    """
    if not self.is_artist_uri(artist):
      artist = self.get_artist_info(artist).get('uri', artist)

    if not self.is_artist_uri(artist):
      self.log('Invalid artist: {}.'.format(artist))
      return []

    related = self.sp.artist_related_artists(artist)
    return [u['uri'] for u in related['artists']]


  def get_album_tracks(self, album, limit=50, offset=0):
    """
    Returns the tracks of an album as a list of uri's

    param album: Spotify album uri or name
    param limit: The number of tracks to return
    param offset: The index of the first album to return
    """
    if not self.is_album_uri(album):
      album = self.get_album_info(album).get('uri', album)

    if not self.is_album_uri(album):
      self.log('Invalid album: {}.'.format(album), level='WARNING')
      return []

    results = self.sp.album_tracks(album, limit=limit, offset=offset)
    return [t['uri'] for t in results['items']]


  def get_artist_albums(self, artist, album_type=None, country=None, limit=20, offset=0):
    """
    Returns albums by the given artist as a list of uri's

    param artist: Spotify artist uri or name
    param album_type: One of 'album', 'single', 'appears_on', 'compilation'
    param country: Limit responce to a particular country
    param limit: The number of albums to return
    param offset: The index of the first album to return (1 - 50)
    """ 
    valid_album_types = ['album', 'single', 'appears_on', 'compilation']
    if album_type and album_type not in valid_album_types:
      self.log('Invalid album_type: {}, setting album_type to None.'.format(album_type), level='WARNING')

    if not self.is_artist_uri(artist):
      artist = self.get_artist_info(artist).get('uri', artist)

    if not self.is_artist_uri(artist):
      self.log('Invalid artist: {}.'.format(artist))
      return []
    
    results = self.sp.artist_albums(artist, album_type=album_type, country=(country or self._country), limit=limit, offset=offset)
    return [a['uri'] for a in results['items']]


  def get_current_user_saved_tracks(self):
    """
    Returns saved tracks from the current user as a list of track uri's
    """
    res = self.sp.current_user_saved_tracks()
    return [u['track']['uri'] for u in res['items']]


  def get_all_playlist_tracks_for_user(self, username='me', include_playlist=None, exclude_playlist=None):
    """
    Returns all playlist tracks for a user as a list of uri's

    param username: Spotify username
    param include: Name or uri of playlists to include in the results
    param exclude: Name or uri of playlists to exclude in the results
    """
    if include_playlist and exclude_playlist:
      self.log('Cannot specify both include and exclude playlists.', level='WARNING')
      return []

    if isinstance(exclude_playlist, str):
      exclude_playlist = [exclude_playlist]
    if isinstance(include_playlist, str):
      include_playlist = [include_playlist]

    username = self._map_spotify_usernames(username)
    # playlists = [self.get_playlist_info(pl, username)['tracks'] for pl in self.get_playlists(username, include_playlist, exclude_playlist)]
    # return [track for tracks in playlists for track in tracks]

    res = []
    for pl in self.get_playlists(username, include_playlist, exclude_playlist):
      for track in self.get_playlist_info(pl, username)['tracks']:
        res.append(track)
    return res


  def get_playlists(self, username='me', include=None, exclude=None):
    """
    Returns playlists owned by a given user as a list or uri's

    param username: name of user to find playlists for
    param include: Name or uri of playlists to include in the results
    param exclude: Name or uri of playlists to exclude in the results
    """
    if include and exclude:
      self.log('Cannot specify both include and exclude.', level='WARNING')
      return []
    
    if include and isinstance(include, str):
      include = [include]
    if exclude and isinstance(exclude, str):
      exclude = [exclude]

    username = self._map_spotify_usernames(username)
    playlists = self.sp.user_playlists(username)

    if include:
      return [pl['uri'] for pl in playlists['items'] if pl['name'] in include or pl['uri'] in include]
    elif exclude:
      return [pl['uri'] for pl in playlists['items'] if pl['name'] not in exclude and pl['uri'] not in exclude]
    else:
      return [pl['uri'] for pl in playlists['items']]


  def get_current_user_playlists(self):
    """
    Returns playlists from the user whose credentials were used in the config as a list or uri's
    Use get_playlists('my_username') as an alternative
    """
    results = self.sp.current_user_playlists(limit=50)
    return [u['uri'] for u in results['items']]


  def get_tracks_from_playlist(self, uri):
    """
    Returns the tracks of a playlist as a list or uri's
    
    param uri: Spotify playlist uri
    """
    return self.get_playlist_info(uri).get('tracks', [])


  def get_playlist_info(self, playlist, username='me'):
    """
    Returns playlist info as a dictionary

    param playlist: Spotify playlist uri
    param username: The user that the playlist belongs to
    """
    if not self.is_playlist_uri(playlist):
      self.log('Invalid playlist: {}.'.format(playlist), level='WARNING')
      return {}
    
    username = self._map_spotify_usernames(username)
    playlists = self.sp.user_playlist(username, playlist)

    return {
      'name' : playlists['name'],
      'uri' : playlists['uri'],
      'owner_name' : playlists['owner']['display_name'],
      'owner_id' : playlists['owner']['id'],
      'description' : playlists['description'],
      'num_tracks' : playlists['tracks']['total'],
      'tracks' : [t['track']['uri'] for t in playlists['tracks']['items']],
    }


  def get_track_info(self, track, artist=None):
    """
    Returns track info as a dictionary

    param track: Spotify track uri or name
    param artist: Spotify artist uri or name
    """
    if not self.is_track_uri(track):
      results = None
      if artist:
        results = self.sp.search(q='artist:' + artist + ' track:' + track, type='track', limit=1)
      else:
        results = self.sp.search(q='track:' + track, type='track', limit=1)

      # Check if we found a result
      if results['tracks']['items']:
        track = results['tracks']['items'][0]['uri']
      else:
        return {}

    result = self.sp.track(track)
    return {
      'uri' : track,
      'name' : result['name'],
      'artist' : result['album']['artists'][0]['name'], # This will only get the first artist (potentially multiple per song)
      'artist_uri' : result['album']['artists'][0]['uri'],
      'album_name' : result['album']['name'],
      'album_uri' : result['album']['uri'],
    }


  def get_artist_info(self, artist):
    """
    Returns artist info as a dictionary

    param artist: Artist uri or name, album uri, or track uri from the desired artist
    """
    if self.is_track_uri(artist):
      artist_uri = self.get_track_info(artist)['artist_uri']
    elif self.is_album_uri(artist):
      artist_uri = self.get_album_info(artist)['artist_uri']
    else:
      artist_uri = artist

    if not self.is_artist_uri(artist_uri):
      results = self.sp.search(q='artist:' + artist_uri, limit=1, type='artist')
      if results['artists']['items']:
        artist_uri = results['artists']['items'][0]['uri']

    if not self.is_artist_uri(artist_uri):
      self.log('Invalid artist: {}.'.format(artist), level='WARNING')
      return {}

    results = self.sp.artist(artist_uri)
    return {
      'name' : results['name'],
      'uri' : results['uri'],
      'genres' : results['genres'],
    }

  
  def get_album_info(self, album, artist=None):
    """
    Returns album info as a dictionary

    param album: Spotify album uri or name
    param artist: Spotify artist uri or name
    """
    if not self.is_album_uri(album):
      results = None
      if artist:
        results = self.sp.search(q='album:' + album + ' artist:' + artist, type='album', limit=1)
      else:
        results = self.sp.search(q='album:' + album, type='album', limit=1)
      album = results['albums']['items'][0]['uri']
    
    result = self.sp.album(album)
    return {
      'uri' : album,
      'num_tracks' : result['total_tracks'],
      'name' : result['name'],
      'artist' : result['artists'][0]['name'], # This will only get the first artist (potentially multiple per song)
      'artist_uri' : result['artists'][0]['uri'],
      'tracks' : [t['uri'] for t in result['tracks']['items']],
    }

  ######################   MUSIC RECOMMENDATION HELPER METHODS END   ########################


  ######################   SPOTIFY PLAY EVENT HANDLING METHODS   ########################

  def _spotify_play_event_callback(self, event_name, data, kwargs):
    """
    Handles the play event - play a spotify song to a Spotiy device using an event fired from HA or AD
    """
    d = data
    
    device = d.get('device', None)
    if not device:
      self.log('Please specify a device.', level='WARNING')
      return

    random_start = True if d.get('random_start', False) else False 
    shuffle = True if d.get('shuffle', False) else False
    repeat = d.get('repeat', 'off')
    if repeat not in ['track', 'context', 'off']:
      self.log("Invalid repeat state specified: {}, choose one of 'track', 'context', 'off'. Repeat set to 'off'.".format(repeat), level='WARNING')
      repeat = 'off' 

    to_play = self.get_recommendation(data)

    if to_play:
      offset = None
      if random_start:
        self.log('Random start is turned on.', level=self.DEBUG_LEVEL)
        offset = self._get_random_offset(to_play)
      self.play(device, to_play, offset)
      self.repeat(repeat)
      if repeat != 'off': self.log('Repeat is turned on to "{}".'.format(repeat))
      self.shuffle(shuffle)
      if shuffle: self.log('Shuffle is turned on.')
    else:
      self.log('Nothing was found matching your parameters. No music will play.', level='INFO')


  def get_recommendation(self, data):
    """
    Make a music recommendation based on the input data

    param data: Dictionary containing user parameters (See the documentation)
    """
    d = data

    random_search = True if d.get('random_search', False) else False
    similar = True if d.get('similar', False) else False
    single = True if d.get('single', False) else False
    multiple = True if d.get('multiple', False) else False

    try:
      num_tracks = int(d.get('tracks', 0))
    except ValueError:
      self.log('Please specifiy a number for "tracks".', level=self.DEBUG_LEVEL)
      num_tracks = 0

    to_play = None
    if not similar:
      # Check if a uri was passed in
      to_play = self._get_media_from_uri(data)
    if not to_play:
      # Nothing was found yet, make a recommendation
      to_play = self._get_recommendation(data)

    if to_play:
      if single:
        self.log('A single song has been requested to play.', level=self.DEBUG_LEVEL)
        to_play = self.get_single_track(to_play, random_search)
      elif multiple:
        self.log('Multiple songs have been requested to play.', level=self.DEBUG_LEVEL)
        to_play = self.get_multiple_tracks(to_play)
      elif num_tracks > 0: # User specified a specific number of tracks they would like to hear
        self.log('"{}" tracks have been requested to play.'.format(num_tracks), level=self.DEBUG_LEVEL)
        to_play = self.get_number_of_tracks(to_play, num_tracks, similar, random_search)

    return to_play


  def _get_media_from_uri(self, data):
    """
    Checks for a Spotify uri in the data and return the track/playlist/artist/album uri if found

    Priority order: track -> playlist -> album -> artist

    param data: Dictionary containing user parameters (See the documentation)
    """
    d = data 

    track = d.get('track', None)
    playlist = d.get('playlist', None)
    album = d.get('album', None)
    artist = d.get('artist', None)

    multiple = True if d.get('multiple', False) else False
    random_search = True if d.get('random_search', False) else False

    to_play = None

    if track:
      valid = True
      if isinstance(track, list):
        for u in track:
          if not self.is_track_uri(u):
            valid = False
      elif not self.is_track_uri(track):
        valid = False
      elif self.is_track_uri(track) and multiple: # User wants multiple songs
        return ''
      if valid:
        self.log("Found a track uri or list of track uri's.", level=self.DEBUG_LEVEL)
        to_play = track
    elif playlist:
      if self.is_playlist_uri(playlist):
        self.log('Found a playlist uri.', level=self.DEBUG_LEVEL)
        to_play = playlist
    elif album:
      if self.is_album_uri(album):
        self.log('Found a album uri.', level=self.DEBUG_LEVEL)
        to_play = album
    elif artist:
      if self.is_artist_uri(artist):
        self.log('Found a artist uri.', level=self.DEBUG_LEVEL)
        to_play = artist
        if random_search:
          albums = self.get_artist_albums(artist)
          to_play = random.choice(albums)

    return to_play


  def _get_recommendation(self, data):
    """
    Returns a Spotify recommendation based on user input data

    Priority order: user defined playlist -> track defined -> album defined -> artist defined -> genre -> category -> 
      -> featured playlist -> newly released album -> users playlist (nothing defined - fallback)

    param data: Dictionary containing user parameters (See the documentation)
    """
    d = data

    track = d.get('track', None)
    playlist = d.get('playlist', None)
    album = d.get('album', None)
    artist = d.get('artist', None)

    random_search = True if d.get('random_search', False) else False
    user = d.get('username', 'me')
    genre = d.get('genre', None)
    category = d.get('category', None)
    featured = True if d.get('featured', False) else False
    new_releases = True if d.get('new_releases', False) else False
    single = True if d.get('single', False) else False
    multiple = True if d.get('multiple', False) else False
    similar = True if d.get('similar', False) else False

    to_play = None

    # Use the user defined 'playlist' parameter to find music
    if playlist:
      self.log('Attempting to use the playlist name to find a user playlist.', level=self.DEBUG_LEVEL)
      pl = self.get_playlists(username=user, include=playlist)
      if pl:
        if random_search:
          to_play = random.choice(pl)
        else:
          to_play = pl[0]

    # Use the user defined 'track' parameter to find music
    if not to_play and track:
      if not similar:
        self.log('Attempting to use the track name to find the song.', level=self.DEBUG_LEVEL)
        to_play = self.get_track_info(track, artist).get('uri', None)
      if not to_play:
        self.log('Attempting to use the track name to make a similar track recommendation.', level=self.DEBUG_LEVEL)
        to_play = self.get_spotify_recommendation(tracks=track, genres=genre, artists=artist)

    # Use the user defined 'album' parameter to find music
    if not to_play and album:
      if not similar:
        self.log('Attempting to use the album name to the album.', level=self.DEBUG_LEVEL)
        to_play = self.get_album_info(album, artist).get('uri', None)
      elif similar or not to_play:
        self.log('Attempting to use the album name to find a similar album.', level=self.DEBUG_LEVEL)
        album_info = self.get_album_info(album, artist)
        album_artist = album_info.get('artist_uri', None)
        album_uri = album_info.get('uri', None)

        if album_artist:
          chosen_artist = album_artist
          if random.choice([1,2]) == 1: # Randomly pick a related artist
            self.log('Attemping to use a different artist than the input album artist.', level=self.DEBUG_LEVEL)
            related_artists = self.get_related_artists(album_artist)
            if related_artists:
              if random_search:
                chosen_artist = random.choice(related_artists)
              else:
                chosen_artist = related_artists[0]
          artist_albums = self.get_artist_albums(chosen_artist)
          if album_uri in artist_albums and len(artist_albums) > 1: # Remove the user defined album from the choices
            artist_albums.remove(album_uri)
          if random_search:
            to_play = random.choice(artist_albums)
          else:
            to_play = artist_albums[0]

    # Use the user defined 'artist' parameter to find music
    if not to_play and artist:
      self.log('Attempting to use the artist name to find music.', level=self.DEBUG_LEVEL)
      chosen_artist = artist
      if similar:
        self.log('Attempting to find similar music from the artist.', level=self.DEBUG_LEVEL)
        artist_info = self.get_artist_info(artist)
        similar_artists = self.get_related_artists(artist_info['uri'])
        if similar_artists:
          if random_search:
            chosen_artist = random.choice(similar_artists)
          else:
            chosen_artist = similar_artists[0]

      if single or not multiple:
        to_play = self.get_top_tracks(chosen_artist)
        if random_search:
          to_play += self.get_artist_tracks(chosen_artist, 10, similar, random_search)
          random.shuffle(to_play)
      if (not single and multiple) or not to_play:
        artist_albums = self.get_artist_albums(chosen_artist)
        if artist_albums:
          if random_search:
            to_play = random.choice(artist_albums)
          else:
            to_play = artist_albums[0]

      if not to_play:
        to_play = self.get_spotify_recommendation(artists=artist, genres=genre, tracks=track)

    # Use the user defined 'genre' parameter to find music
    if not to_play and genre:
      self.log('Attempting to use the genre name to make a recommendation.', level=self.DEBUG_LEVEL)
      if genre in self.get_recommendation_genre_seeds():
        to_play = self.get_spotify_recommendation(artists=artist, genres=genre, tracks=track)
      if not to_play:
        self.log('No music found matching your genre, attempting to find a matching category.', level=self.DEBUG_LEVEL)
        to_play = self.get_playlists_by_category(category)
        if to_play:
          if random_search:
            to_play = random.choice(to_play)
          else:
            to_play = to_play[0]
      

    # Use the user defined 'category' parameter to find music
    if not to_play and category:
      self.log('Attempting to use the category name to make a recommendation.', level=self.DEBUG_LEVEL)
      to_play = self.get_playlists_by_category(category)
      if to_play:
        if random_search:
          to_play = random.choice(to_play)
        else:
          to_play = to_play[0]
      if not to_play:
        self.log('No music found matching your category, attempting to find a matching genre.', level=self.DEBUG_LEVEL)
        to_play = self.get_spotify_recommendation(genres=category)

    # Use the user defined 'featured' parameter to find music
    if not to_play and featured:
      self.log('Attempting to find a featured playlist.', level=self.DEBUG_LEVEL)
      to_play = self.get_featured_playlists() # List of playlists
      if not to_play:
        self.log('No featured playlists were found, attempting to find a newly released album.', level=self.DEBUG_LEVEL)
        to_play = self.new_releases() # List of albums
      if to_play:
        if random_search:
          to_play = random.choice(to_play)
        else:
          to_play = to_play[0]

    # Use the user defined 'new_releases' parameter to find music
    if not to_play and new_releases:
      self.log('Attempting to find a newly released album.', level=self.DEBUG_LEVEL)
      to_play = self.new_releases() # List of albums
      if not to_play:
        self.log('No newly released albums were found, attempting to find a featured playlist.', level=self.DEBUG_LEVEL)
        to_play = self.get_featured_playlists() # List of playlists
      if to_play:
        if random_search:
          to_play = random.choice(to_play)
        else:
          to_play = to_play[0]

    # Nothing matches the user defined parameters or none were defined - use the fallback
    if not to_play:
      self.log('No music was found, using the fallback which is a saved user playlist or track.', level=self.DEBUG_LEVEL)
      to_play = self.get_playlists()
      if to_play:
        if random_search:
          to_play = random.choice(to_play)
        else:
          to_play = to_play[0]
      else:
        to_play = self.get_current_user_saved_tracks()

    return to_play


  def get_multiple_tracks(self, uri):
    """
    Ensures the return uri will contain a Spotify uri or list of track uri's that will play multiple songs

    param uri: A valid Spotify uri or list of uri's
    """
    if isinstance(uri, str) and self.is_track_uri(uri):
      tracks = self.get_spotify_recommendation(tracks=uri)
      return [uri] + tracks
    return uri


  def get_number_of_tracks(self, uri, num_tracks, similar=False, random_search=False):
    """
    Returns a list of tracks that is the num_tracks in length using the given uri for recommendations

    The return number of tracks is not a guarantee

    param uri: A valid Spotify uri or list of track uri's
    param num_tracks: Number of tracks to return
    similar: Find tracks from similar artists
    param random_search: Whether or not to randomly choose songs
    """
    res = []

    # Add the provided uri(s) to the start of our result
    if isinstance(uri, list):
      res = uri
      if random_search:
        search_uri = random.choice(uri)
      else:
        search_uri = uri[0]
    else: # single uri passed in
      search_uri = uri
      if self.is_track_uri(uri):
        res.append(uri)
      elif self.is_playlist_uri(uri):
        pl_tracks = self.get_playlist_info(search_uri).get('tracks', [])
        res += pl_tracks
      elif self.is_album_uri(uri):
        album_tracks = self.get_album_info(uri).get('tracks', [])
        res += album_tracks
      # If uri is an artist uri, we will get tracks later

    if len(res) >= num_tracks:
      if random_search:
        return random.sample(res, num_tracks)
      return res[:num_tracks]

    # Determine the artist of the given uri
    search_artist = None
    if self.is_playlist_uri(search_uri):
      tracks = self.get_playlist_info(search_uri)['tracks']
      if tracks:
        track = random.choice(tracks)
        search_artist = self.get_artist_info(track).get('uri', None)
    if not search_artist:
      search_artist = self.get_artist_info(search_uri).get('uri', None)

    # Add artist album tracks until we reach our desired number of tracks
    if search_artist:
      tracks = self.get_artist_tracks(search_artist, num_tracks-len(res), similar, random_search)
      res += tracks
      if len(res) < num_tracks:
        tracks = self.get_artist_tracks(search_artist, num_tracks-len(res), not similar, random_search)
        res += tracks
      if len(res) < num_tracks:
        tracks = self.get_spotify_recommendation(artists=search_artist, limit=num_tracks-len(res))
        res += tracks

    return res


  def get_single_track(self, uri, random_track=False):
    """ 
    Extracts a single track uri from the provided uri
    
    param uri: A valid Spotify uri or list of track uri's
    param random_track: Whether or not to randomly choose the track
    """
    # If a list of tracks is passed in, deal with it
    if isinstance(uri, list):
      if random_track:
        uri = random.choice(uri)
      else:
        uri = uri[0]

    tracks = []
    if self.is_track_uri(uri):
      return uri
    if self.is_playlist_uri(uri):
      tracks = self.get_playlist_info(uri)['tracks']
    if self.is_album_uri(uri):
      tracks = self.get_album_info(uri)['tracks']
    elif self.is_artist_uri(uri):
      albums = self.get_artist_albums(uri)
      if albums:
        if random_track:
          album = random.choice(albums)
        else:
          album = album[0]
        tracks = self.get_album_info(album)['tracks']
    
    # We didn't find anything
    if not tracks:
      return uri

    if random_track:
      return random.choice(tracks)
    else:
      return tracks[0]


  def _get_random_offset(self, uri):
    """ 
    Return a random offset that corresponds to a valid Spotify media type

    Only playlists, albums and a list of tracks can have an offset

    param uri: A valid Spotify uri or list of track uri's
    """
    if isinstance(uri, list):
      nt = len(uri)
    elif self.is_playlist_uri(uri):
      nt = self.get_playlist_info(uri)['num_tracks']
    elif self.is_album_uri(uri):
      nt = self.get_album_info(uri)['num_tracks']
    else:
      return None
    
    return random.randint(0, nt - 1)

  ######################   SPOTIFY PLAY EVENT HANDLING METHODS END   ########################

  def _disconnect_cast(self, cast):
    """ 
    Disconnect Chromecast device from socket connection

    param cast: The Chromecast device to disconnect
    """
    name = cast.name
    try :
      cast.disconnect(timeout=10)
      self.log('Disconnected cast: "{}"'.format(name))
    except Exception as e:
      # self.log('Failed to disconnect "{}", error: {}'.format(name, e), level='WARNING')
      pass


  def _disconnect_casts(self):
    """ 
    Disconnect all discovered Chromecast devices from socket connection
    """
    for cast in self._chromecasts:
      self._disconnect_cast(cast.get_cast())


  def terminate(self):
    # self._disconnect_casts()
    pass



class CastDevice:
  """Representation of a Cast device on the network.

  Code from: https://github.com/home-assistant/home-assistant/blob/dev/homeassistant/components/cast/media_player.py

  This class is the holder of the pychromecast.Chromecast object and its socket client.
  """

  def __init__(self, chromecast, logger, debug_level='DEBUG'):
    self._chromecast = None # pychromecast.Chromecast
    self._cast_info = {} 
    self.cast_status = None
    self.media_status = None
    self.connection_status = None
    self._available = False
    self._status_listener = None
    self.logger = logger
    self._debug_level = debug_level

    self.set_cast(chromecast)

  @property
  def name(self):
    return self._cast_info.get('friendly_name', None)

  @property
  def model_name(self):
    return self._cast_info.get('model_name', None)

  @property
  def uuid(self):
    return self._cast_info.get('uuid', None)

  @property
  def host(self):
    return self._cast_info.get('host', None)

  @property
  def port(self):
    return self._cast_info.get('port', None)

  @property
  def available(self):
    """ Return True if the cast device is connected or connecting """
    return self._available or self.connection_status == CONNECTION_STATUS_CONNECTING

  @property
  def complete_info(self):
    return all([self.name, self.host, self.port, self.model_name, self.uuid])

  def get_cast(self):
    return self._chromecast

  def reset_cast_connection(self):
    """
    Attempt to initialize a new cast connection after disconnection
    """
    if not self.complete_info():
      self.logger.log('Incomplete cast information ({}), skipping reconnection attempt.'.format(self.name), self._debug_level)
      return

    info = (self.host, self.port, self.uuid, self.model_name, self.name)
    chromecast = pychromecast._get_chromecast_from_host(info, tries=5, retry_wait=1, timeout=30)
    self.set_cast(chromecast)

  def set_cast(self, chromecast):
    """ 
    Initially setup using the cast device

    param chromecast: pychromecast.Chromecast device
    """
    if self._chromecast is not None:
      # The chromecast is already setup
      self.logger.log('Chromecast is already setup: {}'.format(self._cast_info.friendly_name), self._debug_level)
      return

    self._cast_info['host'] = chromecast.host
    self._cast_info['port'] = chromecast.port
    self._cast_info['friendly_name'] = chromecast.device.friendly_name
    self._cast_info['model_name'] = chromecast.device.model_name
    self._cast_info['manufacturer'] = chromecast.device.manufacturer
    self._cast_info['uuid'] = chromecast.device.uuid
    self._cast_info['cast_type'] = chromecast.device.cast_type

    self._chromecast = chromecast

    self._status_listener = CastStatusListener(self, chromecast, self.logger, self._debug_level)

    # Assume connection is successful until told otherwise
    self._available = True

  def new_cast_status(self, cast_status):
    """ Handle updates of the cast status """
    self.cast_status = cast_status
    # self.logger.log('Received new cast device status on: {}'.format(self.name))

  def new_media_status(self, media_status):
    """ Handle updates of the media status """
    self.media_status = media_status
    # self.logger.log('Received new cast device media status on: {}'.format(self.name))

  def new_connection_status(self, connection_status):
    """ Handle updates of connection status """
    self.connection_status = connection_status.status
    self.logger.log(
      "[{} ({}:{})] Received new cast device connection status: {}".format(
      self.name,
      self.host,
      self.port,
      connection_status.status,
    ), self._debug_level)

    if connection_status.status == CONNECTION_STATUS_DISCONNECTED:
      self._available = False
      self._invalidate()
      return

    new_available = connection_status.status == CONNECTION_STATUS_CONNECTED
    if new_available != self._available:
      # Connection status callbacks happen often when disconnected.
      # Only update state when availability changed
      self.logger.log(
        "[{} ({}:{})] Cast device availability changed: {}".format(
        self.name,
        self.host,
        self.port,
        connection_status.status,
      ), self._debug_level)
      self._available = new_available

  def _invalidate(self):
    """ Invalidate some attributes """
    self.logger.log('Cast ({}) is invalidated, reset connection to use it again.'.format(self.name), self._debug_level)
    self._chromecast = None
    self.cast_status = None
    self.media_status = None
    if self._status_listener is not None:
      self._status_listener.invalidate()
      self._status_listener = None


class CastStatusListener:
  """ Helper class to handle pychromecast status callbacks 

  Code from: https://github.com/home-assistant/home-assistant/blob/dev/homeassistant/components/cast/helpers.py

  Necessary because a CastDevice entity can create a new socket client
  and therefore callbacks from multiple chromecast connections can
  potentially arrive. This class allows invalidating past chromecast objects.
  """

  def __init__(self, cast_device, chromecast, logger, debug_level):
    """Initialize the status listener."""
    self._cast_device = cast_device
    self._uuid = chromecast.uuid
    self._valid = True
    self.logger = logger
    self._debug_level = debug_level

    chromecast.register_status_listener(self)
    chromecast.socket_client.media_controller.register_status_listener(self)
    chromecast.register_connection_listener(self)

  def new_cast_status(self, cast_status):
    """Handle reception of a new CastStatus."""
    if self._valid:
      self._cast_device.new_cast_status(cast_status)

  def new_media_status(self, media_status):
    """Handle reception of a new MediaStatus."""
    if self._valid:
      self._cast_device.new_media_status(media_status)

  def new_connection_status(self, connection_status):
    """Handle reception of a new ConnectionStatus."""
    if self._valid:
      self._cast_device.new_connection_status(connection_status)

  def invalidate(self):
    """Invalidate this status listener.
    All following callbacks won't be forwarded.
    """
    self._valid = False