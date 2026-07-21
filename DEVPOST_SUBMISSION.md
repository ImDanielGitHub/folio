# Folio — Devpost submission copy

## Project Story

## Inspiration

I work as an independent contractor, so the line between personal and business spending is not always as clean as it should be.

Some expenses are obvious. Others need a sentence of context that only I know. I might pay for work material on a personal card, use one software subscription across several contracts, or look at a healthy balance without remembering that rent and a stack of renewals are about to land. By the time I sit down to sort everything out, I am often trying to remember why I made a purchase weeks earlier.

I had been following ChatGPT Finance and Perplexity Finance because asking a question is a much more natural way to explore money than building another spreadsheet. Many finance products and bank connections still do not reach New Zealand, though. I saw an opportunity to make something useful straight away for independent contractors, sole traders and small-business owners here.

Privacy mattered too. Handing years of transactions, receipts and business information to an online service is a big commitment. Folio can work with a local model through LM Studio, on the owner's computer. If the computer cannot comfortably run a capable model, the owner can explicitly choose a cloud model instead.

LM Studio's Bionic also shaped the build. Its local agent experience showed how much the application around a model matters: give the model a focused set of tools, catch bad responses and help it recover. I applied those ideas to finance, then hid most of that machinery from the main screen.

Folio is the product I wanted for my own work: I can explain a purchase in ordinary language, ask how the business is doing and get the relevant number, chart or document beside the conversation.

## What it does

Folio opens with a conversation, not a wall of finance widgets.

It imports transaction data, works out what deserves attention and asks one useful question at a time. If an answer needs a chart, transaction, table or document, Folio opens it beside the conversation. Close that view and the app returns to a simple thread.

The demo follows a fictional New Zealand business through one connected piece of work:

- Daily Close finds a duplicate software charge, a MITRE 10 purchase with missing context and the current cash position.
- The owner explains that the MITRE 10 purchase was material for a client fit-out and asks Folio to treat similar purchases under $500 the same way.
- Folio updates the item, saves the explanation for later and offers Undo.
- A cash scenario compares buying a laptop now with deferring it, including the effect on the owner's reserve.
- A Telegram-style message — “Parking for the client meeting, $32.40. Expense it” — adds timely context for the next close.
- Folio prepares an owner pack from the same saved figures, with open questions and cash assumptions included.
- Small source links connect important figures to the bank row, owner explanation or document behind them.

The default demo uses fictional data. Folio also includes optional read-only Akahu support for New Zealand accounts and settled transactions. A Plaid sandbox path demonstrates its read-only Link and transaction flow. Neither connector is attached to a real bank account in the public demo.

For conversation, Folio can use a model loaded in LM Studio or an optional OpenAI model. The financial history stays intact when the owner changes models.

## How I built it

Folio is a standalone Electron app built during OpenAI Build Week. The desktop interface uses React, TypeScript and Vite. A local FastAPI service does the finance work, and SQLite stores conversations, imported records, corrections, findings and generated documents.

The language model does not calculate balances or rewrite financial records by itself. Money is stored as integer minor units, while tested Python code handles totals, duplicate detection, corrections, cash scenarios and owner-pack preparation. The model interprets the request, chooses an appropriate finance tool and explains the result.

That separation also makes smaller local models more useful. Folio selects a short list of tools for each request instead of presenting the whole catalogue. Tool inputs have narrow schemas. If a response is almost valid, Folio gets one bounded repair attempt; repeated or invalid calls are stopped. Supported finance requests can then fall back to the same local operations used by the demo.

Long conversations cannot fit in one prompt forever. Folio keeps the original bank rows, documents and owner messages, then links them to people, businesses, transactions, explanations and later corrections. Each new question receives the relevant part of that history. A correction supersedes the old understanding but does not erase it.

The interface uses a closed set of financial views. A model can request a transaction, cash scenario, records table, work receipt or owner pack, but it cannot send arbitrary interface code to Electron.

LM Studio runs over the computer's loopback connection. The optional cloud path uses the OpenAI Responses API. The owner chooses the route in Privacy & models; Folio does not switch from local to cloud in the background.

I used Codex with GPT-5.6 throughout the week. It helped me investigate finance and local-model products, separate this project from my earlier Hermes Finance experiment, build the API and desktop app, write tests, recover work from failed branches and debug the LM Studio integration. I made the product calls, including a major redesign after the first interface exposed far too much activity and accounting detail.

## Challenges I ran into

The hardest problem was getting useful work from a small local model without trusting a confident-looking answer.

Models can choose the wrong tool, leave out an argument, return nearly valid JSON or repeat an action. I put validation, a small repair budget and loop detection around model calls. The finance service remains responsible for every saved amount and change.

Conversation history was the second challenge. A useful answer can depend on a bank row, a receipt, something the owner said three weeks ago and a correction made yesterday. Sending the complete history on every turn becomes slow and eventually exceeds the model's context window. Folio instead retrieves a smaller current picture while keeping a route back to the original records.

The design took several passes as well. An early version displayed every finding, process stage and evidence record at once. It was technically informative and exhausting to use. The current version keeps the thread calm and opens the extra finance view only when it helps answer the question.

## Accomplishments that I'm proud of

The whole demo runs through one finance service and one saved history: import, Daily Close, duplicate handling, correction, Undo, cash scenarios and owner-pack preparation. The totals in the pack match the figures shown in the conversation.

I am also proud of the continuity. The owner can give a detailed explanation, correct it later, restart the app or change models, and Folio can still use the current version without losing the earlier record.

The local path fails clearly. If LM Studio is unavailable or a model response cannot be used, Folio preserves the last valid financial state. It does not quietly send the data to a cloud provider or show a made-up result.

What makes the project feel finished to me is that it now serves the person I built it for. It helps a contractor deal with the small explanations, timing decisions and loose ends that sit between a bank feed and finished accounts.

## What I learned

A local finance assistant depends as much on its surrounding application as it does on model size. A smaller model does noticeably better with a relevant summary of the business and a few precise tools. Giving it every record and every available action usually makes it worse.

I also learned that financial memory needs dates and sources. “MITRE 10 is normally a business expense” is too broad to be useful. Folio needs to know which purchase the owner explained, what they actually said, how far the new rule should reach and whether they later changed their mind.

The interface lesson was simpler: internal detail is useful for debugging, but owners mostly need to know what happened, why it matters and what to do next. The audit trail is still there when they want to inspect it.

## What's next for Folio

My next step is to put Folio in front of independent contractors and sole traders in New Zealand.

I want to run a small Akahu pilot with read-only accounts, improve receipt matching and connect Telegram so an owner can add context while a purchase is still fresh. Scheduled Daily Close summaries are also on the list, so Folio can prepare the next useful update even when the desktop window is closed.

The cash view currently compares known commitments and alternative dates. Future work could improve recurring-payment detection, learn categories from owner corrections and compare more cash-flow scenarios while keeping every assumption visible.

Folio is free and open source. I want contractors and small businesses to be able to inspect it, adapt it and run it on their own computers.

## Judges' testing instructions

For the full conversational demo, load a local language model in LM Studio and enable its server at `127.0.0.1:1234`. Folio still demonstrates the finance workflow when no model is loaded, but the local model gives the clearest view of the intended experience. You do not need bank credentials or a cloud-model key.

1. Follow the README Quick start, then start the Folio API, renderer and Electron app.
2. Open the example business and run **Daily Close**. It should find the duplicate subscription, the MITRE 10 purchase that needs context and the current cash position.
3. Tell Folio: **“The MITRE 10 purchase was materials for a client fit-out. Treat similar purchases under $500 the same way.”** Open the updated item, then try **Undo**.
4. Ask: **“Show me what happens if I defer the laptop purchase.”** Compare both dates and open the assumptions behind the figures.
5. Open **Evidence**, process the example Telegram message and ask Folio to **prepare the owner pack**. The totals and unresolved items should match the conversation.
6. Open **Privacy & models** to see the active model and the optional OpenAI, Akahu, Plaid and Telegram paths.

Akahu live sync is optional and read-only, and requires the judge's own Akahu configuration. Plaid is an optional sandbox integration. The public demo connects to neither service.

For the full local verification set, run `pnpm contracts:check`, `pnpm lint`, `pnpm typecheck`, `pnpm test`, `pnpm build`, `pnpm demo:golden` and `pnpm eval:offline`.
