# Privacy

This bot connects a Telegram chat to a StudyAtrium account. This page says what it stores and
for how long.

## What the bot stores

Per connected chat, in a SQLite file on the operator's server:

- the Telegram chat id, linked to your StudyAtrium account;
- the API key StudyAtrium issued to the bot for you, **encrypted** at rest;
- the URL of your StudyAtrium instance and your StudyAtrium user id.

While you are connecting (`/login`), a short-lived record (a few minutes) holds the one-time
login state and, after you approve, the pending key and confirmation code. It is deleted when
the connection completes, fails or expires.

The bot keeps no message history, no study data and no logs of what you ask. A timer run you
started from the chat is remembered in memory only, until you stop it or the bot restarts.

## What passes through Telegram

Everything the bot sends you, such as session lists, reminders and course names, is delivered
by Telegram to your own Telegram account, and your commands travel the same way. Telegram
processes that traffic under its own terms and privacy policy; the bot cannot change that. Do
not use the bot in a group chat (it refuses to) and do not connect an account you would not want
to read in Telegram.

## How long it is kept

Until you send `/logout`, or until your StudyAtrium account is deleted. Deleting the account
makes StudyAtrium call the bot, which erases your link and any pending login for it. Sending
`/logout` removes the link at once; the API key then stays valid on StudyAtrium until you revoke
it there.

## Contact

The bot is run by the operator of the StudyAtrium instance you connect to. Requests about your
data go to that operator; their contact details are in the instance's imprint.
