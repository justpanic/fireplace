import logging
import random
import time
from calendar import timegm
from itertools import chain
from .actions import Attack, BeginTurn, Death, Deaths, EndTurn, EventListener
from .card import Card, THE_COIN
from .entity import Entity
from .enums import CardType, PlayState, Step, Zone
from .managers import GameManager
from .utils import CardList


class GameOver(Exception):
	pass


class Game(Entity):
	type = CardType.GAME
	MAX_MINIONS_ON_FIELD = 8
	Manager = GameManager

	def __init__(self, players):
		self.data = None
		super().__init__()
		self.players = players
		for player in players:
			player.game = self
		self.step = Step.BEGIN_FIRST
		self.turn = 0
		self.current_player = None
		self.auras = []
		self.minions_killed = CardList()
		self.minions_killed_this_turn = CardList()
		self._action_queue = []

	def __repr__(self):
		return "<%s %s>" % (self.__class__.__name__, self)

	def __str__(self):
		return "%s vs %s" % (self.players)

	def __iter__(self):
		return self.all_entities.__iter__()

	@property
	def board(self):
		return CardList(chain(self.players[0].field, self.players[1].field))

	@property
	def decks(self):
		return CardList(chain(self.players[0].deck, self.players[1].deck))

	@property
	def hands(self):
		return CardList(chain(self.players[0].hand, self.players[1].hand))

	@property
	def characters(self):
		return CardList(chain(self.players[0].characters, self.players[1].characters))

	@property
	def all_entities(self):
		return CardList(chain(self.entities, self.hands, self.decks))

	@property
	def entities(self):
		return CardList(chain([self], self.players[0].entities, self.players[1].entities))

	@property
	def live_entities(self):
		return CardList(chain(self.players[0].live_entities, self.players[1].live_entities))

	def filter(self, *args, **kwargs):
		return self.all_entities.filter(*args, **kwargs)

	def attack(self, source, target):
		return self.queue_actions(source, [Attack(source, target)])

	def _attack(self):
		"""
		See https://github.com/jleclanche/fireplace/wiki/Combat
		for information on how attacking works
		"""
		attacker = self.proposed_attacker
		defender = self.proposed_defender
		self.proposed_attacker = None
		self.proposed_defender = None
		if attacker.should_exit_combat:
			logging.info("Attack has been interrupted.")
			attacker.should_exit_combat = False
			attacker.attacking = False
			defender.defending = False
			return
		# Save the attacker/defender atk values in case they change during the attack
		# (eg. in case of Enrage)
		def_atk = defender.atk
		attacker.hit(defender, attacker.atk)
		if def_atk:
			defender.hit(attacker, def_atk)
		attacker.attacking = False
		defender.defending = False
		attacker.num_attacks += 1

	def card(self, id):
		card = Card(id)
		self.manager.new_entity(card)
		return card

	def end(self, *losers):
		"""
		End the game.
		\a *losers: Players that lost the game.
		"""
		for player in self.players:
			if player in losers:
				player.playstate = PlayState.LOST
			else:
				player.playstate = PlayState.WON
		raise GameOver("The game has ended.")

	def process_deaths(self):
		return self.queue_actions(self, [Deaths()])

	def _process_deaths(self):
		actions = []
		losers = []
		for card in self.live_entities:
			if card.to_be_destroyed:
				actions.append(Death(card))
				card.ignore_events = True
				if card.type == CardType.MINION:
					self.minions_killed.append(card)
					self.minions_killed_this_turn.append(card)
					card.controller.minions_killed_this_turn += 1
				elif card.type == CardType.HERO:
					card.controller.playstate = PlayState.LOSING
					losers.append(card.controller)

		if losers:
			self.end(*losers)
			return

		if actions:
			self.queue_actions(self, actions)

	def queue_actions(self, source, actions):
		"""
		Queue a list of \a actions for processing from \a source.
		"""
		ret = []
		for action in actions:
			if isinstance(action, EventListener):
				logging.debug("Registering %r on %r", action, self)
				source.controller._events.append(action)
			else:
				self._action_queue.append(action)
				ret.append(action.trigger(source, self))
				self.refresh_auras()
				self._action_queue.pop()
		if not self._action_queue:
			self._process_deaths()

		return ret

	def toss_coin(self):
		outcome = random.randint(0, 1)
		# player who wins the outcome is the index
		winner = self.players[outcome]
		loser = winner.opponent
		logging.info("Tossing the coin... %s wins!" % (winner))
		return winner, loser

	def refresh_auras(self):
		for aura in self.auras:
			aura.update()

	def start(self):
		logging.info("Starting game: %r" % (self))
		self.player1, self.player2 = self.toss_coin()
		self.manager.new_entity(self.player1)
		self.manager.new_entity(self.player2)
		self.current_player = self.player1
		# XXX: Mulligan events should handle the following, but unimplemented for now
		self.player1.cards_drawn_this_turn = 0
		self.player2.cards_drawn_this_turn = 0
		for player in self.players:
			player.zone = Zone.PLAY
			player.summon(player.original_deck.hero)
			for card in player.original_deck:
				card.controller = player
				card.zone = Zone.DECK
			player.shuffle_deck()
			player.playstate = PlayState.PLAYING

		self.player1.draw(3)
		self.player2.draw(4)
		self.begin_mulligan()
		self.player1.first_player = True
		self.player2.first_player = False

	def begin_mulligan(self):
		logging.info("Entering mulligan phase")
		self.step = Step.BEGIN_MULLIGAN
		self.next_step = Step.MAIN_READY
		logging.info("%s gets The Coin (%s)" % (self.player2, THE_COIN))
		self.player2.give(THE_COIN)
		self.begin_turn(self.player1)

	def end_turn(self):
		return self.queue_actions(self, [EndTurn(self.current_player)])

	def _end_turn(self):
		logging.info("%s ends turn %i", self.current_player, self.turn)
		self.step, self.next_step = self.next_step, Step.MAIN_CLEANUP

		self.current_player.temp_mana = 0
		for character in self.current_player.characters.filter(frozen=True):
			if not character.num_attacks:
				character.frozen = False
		for buff in self.current_player.entities.filter(one_turn_effect=True):
			logging.info("Ending One-Turn effect: %r", buff)
			buff.destroy()

		self.step, self.next_step = self.next_step, Step.MAIN_NEXT
		self.begin_turn(self.current_player.opponent)

	def begin_turn(self, player):
		return self.queue_actions(self, [BeginTurn(player)])

	def _begin_turn(self, player):
		self.step, self.next_step = self.next_step, Step.MAIN_START_TRIGGERS
		self.step, self.next_step = self.next_step, Step.MAIN_START
		self.turn += 1
		logging.info("%s begins turn %i", player, self.turn)
		self.step, self.next_step = self.next_step, Step.MAIN_ACTION
		self.current_player = player
		self.minions_killed_this_turn = CardList()

		for p in self.players:
			p.cards_drawn_this_turn = 0
			p.current_player = p is player

		player.turn_start = timegm(time.gmtime())
		player.cards_played_this_turn = 0
		player.minions_played_this_turn = 0
		player.minions_killed_this_turn = 0
		player.combo = False
		player.max_mana += 1
		player.used_mana = player.overloaded
		player.overloaded = 0
		for entity in player.entities:
			if entity.type != CardType.PLAYER:
				entity.turns_in_play += 1
				if entity.type == CardType.HERO_POWER:
					entity.exhausted = False
				elif entity.type in (CardType.HERO, CardType.MINION):
					entity.num_attacks = 0

		player.draw()
