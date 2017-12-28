from flask import url_for

from magic import oracle
from decksite.view import View

# pylint: disable=no-self-use
class Bugs(View):
    def __init__(self):
        self.github_icon = url_for('static', filename='images/github.svg')
        self.cards = oracle.bugged_cards()
        self.tournament_bugs_url = url_for('tournaments', _anchor='bugs')
        self.bug_blog_url = 'https://pennydreadfulmtg.github.io/modo-bugs/bug_blog.html'

    def subtitle(self):
        return 'Bugged Cards'
