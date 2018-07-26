from typing import Dict, Optional

from github import Github
from github.Issue import Issue
from github.IssueComment import IssueComment
from github.Repository import Repository

from shared import configuration, lazy, redis

from . import strings

ISSUE_CODES: Dict[int, str] = {}

@lazy.lazy_property
def get_github() -> Github:
    if not configuration.get_str('github_user') or not configuration.get_str('github_password'):
        return None
    return Github(configuration.get('github_user'), configuration.get('github_password'))

@lazy.lazy_property
def get_repo() -> Repository:
    gh = get_github()
    if gh is not None:
        return gh.get_repo('PennyDreadfulMTG/modo-bugs')
    return None

def create_comment(issue: Issue, body: str) -> IssueComment:
    set_issue_bbt(issue.number, None)
    return issue.create_comment(strings.remove_smartquotes(body))

def set_issue_bbt(number: int, text: Optional[str]) -> None:
    key = f'modobugs:bug_blog_text:{number}'
    if text is None:
        ISSUE_CODES.pop(number, None)
        redis.clear(key)
    else:
        ISSUE_CODES[number] = text
        redis.store(key, text)