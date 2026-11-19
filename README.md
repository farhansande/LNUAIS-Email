# LNU AIS Mailer

Email everyone on the member list, one personal email each. Uses only the Python standard library, so there's nothing to install.

## Run it

```bash
python3 mailer.py
```

Your browser opens at http://127.0.0.1:8765. The page is only reachable from your own computer. Keep the terminal open while sending. Press Ctrl+C to quit.

It reads the member CSV in this folder (it looks for "member" in the filename). To use a different file: `python3 mailer.py --csv path/to/list.csv`.

## Steps on the page

1. **Your email account.** Pick your provider and enter your email and password.
   - **Gmail (recommended):** turn on 2-Step Verification, then create an App Password at https://myaccount.google.com/apppasswords. Paste the 16-character code as the password, not your normal Gmail password.
   - Microsoft 365 school accounts (@student.lnu.se) usually have SMTP sign-in turned off by the university, so use Gmail.
   - Press **Test connection**. It only logs in and sends nothing.
2. **Write the email.** Use plain text or HTML. You can add attachments. Placeholders such as `{first_name}`, `{name}`, `{email}` and `{programme}` are filled in for each person.
3. **Send a test.** Type your own addresses. A test goes **only** to the addresses you type, never to the member list.
4. **Send to the member list.** Untick anyone you want to skip, then type `SEND <number>` to confirm.
   - Tick **Dry run** to go through every member without sending anything.
   - If something fails, a **Retry failed + unsent only** button appears.

Each send writes a log (time, email, status, error) to `sent_logs/`.

## Easiest option: a ready-made draft (no password needed)

Section 5 on the page opens Gmail, Outlook (including your LNU Microsoft 365 account) or your computer's email app. The draft already has every selected member in **BCC** and your subject and message filled in. You check it and press Send yourself.

- Everyone gets the same email: `{first_name}` and `{name}` become "everyone".
- Attachments have to be added in the draft by hand.
- If the list or message is too long for one link, the tool splits it into several drafts or copies the message for you to paste. It tells you when it does this.

## Duplicate and typo handling

The CSV has 160 rows. 12 are duplicate addresses and are skipped automatically, which leaves 148 unique members. One address uses `student.inu.se`, which is probably a typo for `student.lnu.se`. The tool flags it so you can untick it or fix the CSV and press "Reload CSV".

## Tests

```bash
python3 -m unittest -v test_mailer
```

The tests use a fake SMTP server on your own machine, so no real email is sent.
