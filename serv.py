#!/usr/bin/env python
import threading
import tornado.ioloop
import tornado.web
import spotify
import json
import serial
import Queue
#import psycopg2
import math

import logging
#logging.basicConfig(level=logging.DEBUG)
# http://www.tornadoweb.org/en/stable/options.html
from tornado.options import define, options

define("port", default=8888)
#define("debug", default=True)

class PiLiteBoard(threading.Thread):

	def __init__(self, messageQueue):
		threading.Thread.__init__(self)
		self.ser = serial.Serial("/dev/ttyAMA0", baudrate=9600, timeout=0)
		self.ser.write("$$$SPEED40\r")
		self.messageQueue = messageQueue

	def write(self, text):
		text = text.encode('utf-8')
		while text:
			self.ser.write(text[:14])
			text = text[14:]

	def run(self):
		while True:
			message = self.messageQueue.get()
			self.write(message + '  ')
			self.messageQueue.task_done()

class Main(tornado.web.RequestHandler):

	def initialize(self, spotifyHelper):
		self.spotifyHelper = spotifyHelper

	def get_current_user(self):
		return spotifyHelper.session.user

	@tornado.web.authenticated
	def get(self):
		self.write("HELLO " + self.get_current_user().load().display_name)

	def callbackWrapper(self, response):
		self.set_header("Content-Type", "application/json")	
		callback = self.get_argument("callback", "default")

		output = callback + "(" + response + ")" 

		self.write(output)

	def set_default_headers(self):
		self.add_header('Access-Control-Allow-Origin', self.request.headers.get('Origin', '*'))
 
class AuthHandler(Main):

	def initialize(self, spotifyHelper):
		self.spotifyHelper = spotifyHelper

	def login(self):

		username = self.get_argument("username", "")
		password = self.get_argument("password", "")

		spotifyHelper.session.login(username, password, remember_me=True)
		spotifyHelper.logged_in.wait()

		if (spotifyHelper.session.connection.state == spotify.ConnectionState.LOGGED_IN or
		   spotifyHelper.session.connection.state == spotify.ConnectionState.OFFLINE):
			self.set_secure_cookie("user", spotifyHelper.session.user_name)
			self.callbackWrapper("{message:'ok'}")
		else:
			self.callbackWrapper("{message:'Incorrect login'}")

	def logout(self):
		spotifyHelper.session.logout()
		spotifyHelper.logged_out.wait()

		self.set_secure_cookie("user", "")
		self.write("out")

	def unauthed(self):
		self.set_status(403)
		self.write("unauthed!");

	def get(self, action = 'login'):
		if(action == 'login'):
			self.login()
		elif(action == 'logout'):
			self.logout()
	        elif(action == 'unauthed'):
			self.unauthed()
		else:
			self.write('Action `' + action + '` not supported', 400)

class SearchHandler(Main):

	def initialize(self, spotifyHelper):
		self.spotifyHelper = spotifyHelper

	def search(self):

		query = self.get_argument('search', None)

		search = spotifyHelper.session.search(query)
		search.load()

		return {
			'artists': search.artists,
			'tracks': search.tracks,
			'albums': search.albums,
			'playlists': search.playlists
		}

	@tornado.web.authenticated
	def get(self):

		resp = json.dumps(self.search(), cls=SpotifySearchEncoder)
		self.callbackWrapper(resp)


class AudioHandler(Main):

	def initialize(self, spotifyHelper):
		self.spotifyHelper = spotifyHelper
		self.set_header('Access-Control-Allow-Origin', '*')

	def play(self):
		uri = self.get_argument("uri", None)
		idx = self.get_argument('idx', None)

		if uri:
			link = spotifyHelper.session.get_link(uri)
			spotifyHelper.queueHelper.setQueue(link).playQueue()
		elif idx:
			spotifyHelper.queueHelper.playIdx = int(idx)
			spotifyHelper.queueHelper.playQueue()
		elif len(spotifyHelper.queueHelper.queue) > 0:
			spotifyHelper.session.player.play()

		spotifyHelper.queueHelper.playStatus = True

		return True

	def pause(self):
		spotifyHelper.session.player.play(False)

		spotifyHelper.queueHelper.playStatus = False

		return True

	def next(self):
		spotifyHelper.messageQueue.put('Next..')
		return spotifyHelper.queueHelper.next().playIdx

	def prev(self):
		spotifyHelper.messageQueue.put('Previous..')
		return spotifyHelper.queueHelper.prev().playIdx

	def queue(self):
		uri = self.get_argument("uri", None)

		link = spotifyHelper.session.get_link(uri)

		spotifyHelper.queueHelper.loadIntoQueue(link)
		return True

	def volume(self):
		volume = self.get_argument("set", None)

		if(volume):
			spotifyHelper.mixer.setvolume(int(volume))
			spotifyHelper.messageQueue.put('Volume: ' + volume + '%')

		return str(spotifyHelper.mixer.getvolume()[0])

	def nowPlaying(self):
		nowPlaying = {
			"playIdx": spotifyHelper.queueHelper.playIdx,
			"queue": spotifyHelper.queueHelper.queue,
			"volume": self.volume(),
			"playStatus":spotifyHelper.queueHelper.playStatus
		}
		return nowPlaying

	def myPlaylists(self):
		container = spotifyHelper.session.playlist_container
		if not container.is_loaded: container.load()

		return [playlist for playlist in container]


	@tornado.web.authenticated
	def get(self, action):

		resp = ""
		code = 200

		if(action == 'play'):
			resp = self.play()
		elif(action == 'pause'):
			resp = self.pause()
		elif(action == 'volume'):
			resp = self.volume()
		elif(action == 'next'):
			resp = self.next()
		elif(action == 'prev'):
			resp = self.prev()
		elif(action == 'nowplaying'):
			resp = self.nowPlaying()
		elif(action == 'queue'):
			resp = self.queue()
		elif(action == 'playlists'):
			resp = self.myPlaylists()
		else: 
			resp = 'Action `' + action + '` not supported'
			code = 400
		
		if resp:
			resp = json.dumps(resp, cls=SpotifyDetailsEncoder)
		else: 
			output = "{OK:true}"
	
		self.set_status(code)
		self.callbackWrapper(resp)



class QueueHelper(object):

	def __init__(self, spotifyHelper):

		self.spotifyHelper = spotifyHelper
		self.__queue = list()
		self.__playIdx = 0
		self.__playStatus = False
		self.playing_link = None

	def togglePlayStatus(self):
		self.__playStatus = not self.__playStatus
		return self.__playStatus

	def resetQueue(self):
		self.__queue = list()

	def resetPlayIdx(self):
		self.__playIdx = 0

	def setQueue(self, link):

		self.__queue = list()
		self.__playIdx = 0

		self.loadIntoQueue(link)

		return self

	def loadIntoQueue(self, link):

		if(link.type == spotify.LinkType.TRACK):

			track = link.as_track()
			self.addToQueue(track)

		if(link.type == spotify.LinkType.ALBUM):

			album = link.as_album()
			browser = album.browse().load()
			for track in browser.tracks:
				self.addToQueue(track)

		if(link.type == spotify.LinkType.PLAYLIST):

			playlist = link.as_playlist()
			playlist.load()
			for track in playlist.tracks:
				self.addToQueue(track)

			playlist.on(spotify.PlaylistEvent.TRACKS_ADDED, self.tracksAddedToPlaylist)
			playlist.on(spotify.PlaylistEvent.TRACKS_REMOVED, self.trackRemovedFromPlaylist)

		self.playing_link = link

	def tracksAddedToPlaylist(self, playlist, tracks, index):
		if not playlist.is_loaded: playlist.load()

		if(self.__playIdx >= index):
			print("Track added before currently playing!")
			self.__playIdx = self.__playIdx + len(playlist.tracks)

		self.resetQueue()
		for track in playlist.tracks:
			self.addToQueue(track)

	def trackRemovedFromPlaylist(self, playlist, indexs):
		if not playlist.is_loaded: playlist.load()

		self.resetQueue()
		for track in playlist.tracks:
			self.addToQueue(track)

	def addToQueue(self, track):
		if not track.is_loaded: track.load()
		self.__queue.append(track)

	def playQueue(self):
		if(self.__queue[self.__playIdx].availability != spotify.TrackAvailability.AVAILABLE): self.next()
		track = self.__queue[self.__playIdx]
		spotifyHelper.session.player.unload()
		spotifyHelper.session.player.load(track)

		trackAndArtist = track.name + ' - ' + ", ".join([artist.load().name for artist in track.artists])
			
		spotifyHelper.messageQueue.put(trackAndArtist)
		spotifyHelper.session.player.play()

		return self

	def next(self):
		if(self.isNext()):
			self.__playIdx += 1
			if(self.__queue[self.__playIdx].availability != spotify.TrackAvailability.AVAILABLE): self.next()
			self.playQueue()

		return self

	def prev(self):
		if(self.isPrev()):
			self.__playIdx -= 1
			if(self.__queue[self.__playIdx].availability != spotify.TrackAvailability.AVAILABLE): self.prev()
			self.playQueue()

		return self

	def isNext(self):
		return ((len(self.__queue) - 1) > self.__playIdx)

	def isPrev(self):
		return (self.__playIdx > 0)

	@property
	def playIdx(self):
		return self.__playIdx

	@playIdx.setter
	def playIdx(self, value):
		self.__playIdx = value

	@property
	def queue(self):
		return self.__queue

	@queue.setter
	def queue(self, value):
		self.__queue = value

	@property
	def playStatus(self):
		return self.__playStatus

	@playStatus.setter
	def playStatus(self, value):
		self.__playStatus = value

class SpotifyHelper(object):

	def __init__(self):

		self.__logged_in = threading.Event()
		self.__logged_out = threading.Event()
		self.__logged_out.set()

		config = spotify.Config()
		config.user_agent = "Richard's Sonos Raspberry Pi Replacement"

		self.__session = spotify.Session(config=config)

		self.__session.preferred_bitrate(spotify.Bitrate.BITRATE_320k)

		self.__session.on(spotify.SessionEvent.LOGGED_IN, self.on_logged_in)
		self.__session.on(spotify.SessionEvent.LOGGED_OUT, self.on_logged_out)
		self.__session.on(spotify.SessionEvent.END_OF_TRACK, self.on_end_of_track)
		self.__session.on(spotify.SessionEvent.CONNECTION_STATE_UPDATED, self.connection_state_listener)

		self.__audio = spotify.AlsaSink(self.session)
		self.__mixer = self.__audio._alsaaudio.Mixer('PCM')

		self.__queueHelper = QueueHelper(self)

		self.__messageQueue = Queue.Queue()

		self.__event_loop = spotify.EventLoop(self.__session)
		self.__event_loop.start()

	def on_logged_in(self, session, error_type):
		print("login!")
		# TODO Handle error situations
		self.__logged_in.set()
		self.__logged_out.clear()

	def on_logged_out(self, session):
		print("logout!")
		self.__queueHelper.resetQueue()
		self.__queueHelper.resetPlayIdx()
		self.__logged_in.clear()
		self.__logged_out.set()

	def on_end_of_track(self, session):

		if(self.__queueHelper.isNext()):
			self.__queueHelper.next().playQueue()
		else:
			session.player.unload()
			self.__queueHelper.playStatus = False

	def connection_state_listener(self, session):
		if session.connection.state is spotify.ConnectionState.LOGGED_IN:
			name = session.user.load().display_name
			self.__messageQueue.put('Welcome ' + name)


	@property
	def session(self):
		return self.__session
	
	@session.setter
	def session(self, value):
		self.__session = value

	@property
	def logged_in(self):
		return self.__logged_in

	@logged_in.setter
	def logged_in(self, value):
		self.__logged_in = value

	@property
	def logged_out(self):
		return self.__logged_out

	@logged_out.setter
	def logged_out(self, value):
		self.__logged_out = value

	@property
	def event_loop(self):
		return self.__event_loop

	@event_loop.setter
	def event_loop(self, value):
		self.__event_loop = value

	@property
	def mixer(self):
		return self.__mixer

	@mixer.setter
	def mixer(self, value):
		self.__mixer = value

	@property
	def queueHelper(self):
		return self.__queueHelper

	@queueHelper.setter
	def queueHelper(self, value):
		self.__queueHelper = value

	@property
	def messageQueue(self):
		return self.__messageQueue

	@messageQueue.setter
	def  messageQueue(self, value):
		self.__messageQueue = value

class RadioHelper():

	def __init__(self, messageQueue):
		self.messageQueue = messageQueue

	def getStations(self):
		try:
			con = psycopg2.connect(database='sponos', user='richard')
			cur = con.cursor()
			cur.execute('SELECT * FROM radio ORDER BY id')
			radio_list = cur.fetchall()
			return radio_list
		except psycopg2.DatabaseError as e:
			return e
		finally:
			if con: con.close()

class SpotifyDefaultEncoder(json.JSONEncoder):

	def __init__(self, *args, **kwargs):
		json.JSONEncoder.__init__(self, *args, **kwargs)

	def durationFormatter(self, duration):

		 seconds = int(math.floor((duration/1000)%60))
		 minutes = int(math.floor((duration/(1000*60))%60))
		 hours = int(math.floor((duration/(1000*60*60))%24))

		 return self.addZ(minutes) + ":" + self.addZ(seconds)

	def addZ(self, n):
		return "0" + str(n) if n < 10 else str(n)


class SpotifyDetailsEncoder(SpotifyDefaultEncoder):

	def __init__(self, *args, **kwargs):
		SpotifyDefaultEncoder.__init__(self, *args, **kwargs)
		self._cached_nodes = list()

	def default(self, obj):

		if isinstance(obj, spotify.utils.Sequence) or isinstance(obj, list):
			return [x for x in obj]

		#Tracks
		elif isinstance(obj, spotify.track.Track):

			if not obj.is_loaded: obj.load()

			return {
				"name": obj.name,
				"duration": self.durationFormatter(obj.duration),
				"album":obj.album,
				"index": obj.index - 1,
				"disc": obj.disc,
				"artists": ", ".join([artist.load().name for artist in obj.artists]),
				"availability": obj.availability
			}

		# Albums
		elif isinstance(obj, spotify.album.Album):

			if not obj.is_loaded: obj.load()

			return {
				"name": obj.name,
				"artist": obj.artist,
				"year":obj.year
			}

		# Artists
		elif isinstance(obj, spotify.artist.Artist):

			if not obj.is_loaded: obj.load()

			return {
				"name": obj.name,
			}

		# User
		elif isinstance(obj, spotify.user.User):

			if not obj.is_loaded: obj.load()

			return {
				"canonical_name": obj.canonical_name,
				"display_name":obj.display_name
			}

		# Playlists
		elif isinstance(obj, spotify.playlist.Playlist):

			if not obj.is_loaded: obj.load()

			return {
				"name": obj.name,
				"link": obj.link.uri,
				"owner": obj.owner
			}

		# Let the base class default method raise the TypeError
		return json.JSONEncoder.default(self,obj)

class SpotifySearchEncoder(SpotifyDefaultEncoder):

	def __init__(self, *args, **kwargs):
		SpotifyDefaultEncoder.__init__(self, *args, **kwargs)
		self._cached_nodes = list()

	def default(self, obj):

		if isinstance(obj, spotify.utils.Sequence) or isinstance(obj, list):
			return [x for x in obj]

		#Playlist
		elif isinstance(obj, spotify.Playlist):
			if not obj.is_loaded: obj.load()
			return {
				"name": obj.name
			}

		#SearchPlaylists
		elif isinstance(obj, spotify.SearchPlaylist):
			#if not obj.image.is_loaded: obj.image.load()
			return {
				"name": obj.name,
				"link": obj.uri,
				#"cover": obj.image.data_uri
			}

		#Tracks
		elif isinstance(obj, spotify.track.Track):
			if not obj.is_loaded: obj.load()
			return {
				"name": obj.name,
				"duration": self.durationFormatter(obj.duration),
				"index": obj.index - 1,
				"disc": obj.disc,
				"link": obj.link.uri,
				"artists": ", ".join([artist.load().name for artist in obj.artists]),
				"album": obj.album
			}

		# Albums
		elif isinstance(obj, spotify.album.Album):

			cover = ""

			if not obj.is_loaded: obj.load()
			if obj.cover():
				if not obj.cover().is_loaded: 
					cover = obj.cover().load().data_uri
					

			return {
				"name": obj.name,
				"artist": obj.artist,
				"year": obj.year,
				"link": obj.link.uri,
				"cover": cover
			}

		# Artists
		elif isinstance(obj, spotify.artist.Artist):

			if not obj.is_loaded: obj.load()

			return {
				"name": obj.name,
				"link": obj.link.uri
			}

		# Let the base class default method raise the TypeError
		return json.JSONEncoder.default(self,obj)

if __name__ == "__main__":

	spotifyHelper = SpotifyHelper()
	piLite = PiLiteBoard(spotifyHelper.messageQueue)
	piLite.daemon = True
	piLite.start()
	spotifyHelper.messageQueue.put('Ready..')
	
	settings = {
		"cookie_secret":"61oETzKXQAGaYdkL5gEmGeJJFuYh7EQnp2XdTP1o/Vo=",
		"login_url":"/auth/unauthed/",
		"debug":"True",
	}

	handlers = [
		(r"/", Main, dict(spotifyHelper = SpotifyHelper)),
		(r"/auth/([A-z]+)/", AuthHandler, dict(spotifyHelper = SpotifyHelper)),
		(r"/audio/([A-z]+)/", AudioHandler, dict(spotifyHelper = SpotifyHelper)),
		(r"/search/", SearchHandler, dict(spotifyHelper = SpotifyHelper)),
	]
	
	application = tornado.web.Application(handlers, **settings)
	application.listen(options.port)

	try: 
		tornado.ioloop.IOLoop.instance().start()
	except KeyboardInterrupt:
		spotifyHelper.messageQueue.put('Ending..')
		print("Got ^C")
		spotifyHelper.session.logout()
		spotifyHelper.logged_out.wait()
		print("bye!")
