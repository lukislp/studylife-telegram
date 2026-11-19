# Privacy

This bot connects a Telegram chat to a StudyAtrium account. This page says what it stores and
for how long.

## What the bot stores

Per connected chat, in a SQLite file on the operator's server:

- the Telegram chat id, linked to your StudyAtrium account;
- the API key StudyAtrium issued to the bot for you, **encrypted** at rest;
- the URL of your StudyAtrium instance and your StudyAtrium user id.

While you are connecting (`/login`), a short-lived record (ten minutes) holds the one-time
login state and, after you approve, the pending key and confirmation code. It is deleted when
the connection completes or fails. If you never finish, it is no longer usable after ten minutes
and the bot deletes it the next time it starts or another login begins; until then it stays in
the file.

The bot keeps no message history and no study data, and it does not log what you ask or what it
answers. Its operational log (standard output of the container, kept as long as the operator
keeps container logs) does contain the Telegram chat id in a few lines: when an update comes from
a chat that is not allowed, when a message to a chat could not be delivered, and when the session
list of a chat could not be refreshed. The bot does not write API keys, login codes or message
text to it, and the web server's access log lists requests without the login assertion. A timer run you started from the chat is remembered in memory only, until you stop it or
the bot restarts.

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
it there. If you revoke the key on StudyAtrium instead, the bot removes the link the next time
StudyAtrium rejects the key (on your next command or the next reminder check), and the
deletion call of a deleted account also removes a link by your user id, not only by key.

## Controller and contact

- **Controller:** the operator of your instance. Requests about your data go to that operator;
  their contact details are in the instance's imprint.
- **Publisher:** see the imprint at https://lktec.org/impressum
