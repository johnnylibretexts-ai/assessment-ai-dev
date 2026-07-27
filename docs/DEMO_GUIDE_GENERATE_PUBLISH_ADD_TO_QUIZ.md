# AI question → ADAPT quiz

Sign in as `demo@libretexts.dev`. Ask the owner for the password — it is in `handoff-july-6.md`,
never in this repo.

**One password covers both sites.** Both go through LibreOne SSO, so signing in at Assessment AI
usually signs you in at ADAPT too. If ADAPT does prompt, use **Sign in with LibreTexts** — not the
email/password box, which is a separate ADAPT-only credential.

## Generate

1. **https://assess-ai.libretexts.dev** → paste a page URL. Start with one of these two — they
   match the demo courses:

   | book | pages | good for |
   |---|---|---|
   | [Chemistry 2e (OpenStax)](https://chem.libretexts.org/Bookshelves/General_Chemistry/Chemistry_2e_(OpenStax)) | 230 | Demo Chemistry |
   | [Concepts in Biology (OpenStax)](https://bio.libretexts.org/Bookshelves/Introductory_and_General_Biology/Concepts_in_Biology_(OpenStax)) | 64 | Demo Biology |

   Any page inside them works — open the book, click a section, copy the URL from your address bar.

   Two more books also publish, if you need them: **Fundamentals of General, Organic and Biological
   Chemistry** (252 pages) and **Mathematical Methods in Chemistry** (5). Four books, 551 pages
   total.

2. Set **Total number of items** to `2`. Leave mode on **Auto mix**.
3. Click **Generate**. Usually 20–40 s. Reloading is safe — it's a durable job, and the page says so.
4. Click **Review generated item**. The button only appears once generation has genuinely finished.

## Approve — three gates, in this order

Each gate blocks the next, and the **Publish** button stays disabled until all three are done.

5. **Confirm the metadata.** Tick both confirmations — *"… is accurate for the work the learner must
   do"* (Bloom level) and *"… is accurate for the intended learner"* (difficulty) — then click
   **Approve question revision**. ← the step everyone gets wrong

   Miss it and you get a red banner: *"Confirm both the Bloom level and difficulty before approving
   this draft."* The Approve button simply won't take.

6. **Approve the hints.** Tick all three *"I verified the … hint, its citation, and that it does not
   reveal the answer"* boxes, then **Approve saved hint version**. All three are required, and hint
   approval is separate from question approval.

7. **Publish.** Tick **I confirmed this exact source-to-topic alignment**, then **Publish approved
   revision to ADAPT**. Note the **ADAPT question ID** it reports — you'll want it in step 11.

## Add to a quiz

8. **https://adapt.libretexts.dev** → open **Demo Chemistry** or **Demo Biology** → an assignment →
   **Add Questions**.
9. Set question source to **Search Questions**. ← the other step everyone gets wrong
10. Search by **Author** `LibreTexts Assessment AI` → **Update Results**.
11. Find the row whose **ADAPT ID** matches the ID from step 7, then click the blue **+** in that
    row's **Action** column.

    Match on the ID, not the title. The author filter returns **every** item ever published by
    Assessment AI, and titles from the same book look nearly identical — picking by eye is how you
    add the wrong question.

    On a narrow window the table is wider than the page and the **+** sits off-screen to the right —
    scroll the results table sideways to reach it. Clicking the question title does nothing.
    A red **−** instead of a **+** means it's already in this assignment.

Done — it appears at the bottom of the assignment's question list.

## View as a student

12. Go to `https://adapt.libretexts.dev/students/assignments/<id>/summary`
    (`<id>` is in the assignment URL). **Don't click Logout.**

    Expect the due date to read **"This assignment is due ."** with nothing after it. That is normal
    on this page and does **not** mean the assignment is misconfigured. ADAPT resolves the due date
    per enrolled student, and you are viewing as an instructor, so there is no date to fill in. The
    real date is on the instructor assignment list. Same reason a few other student-only fields are
    blank here.

---

## If something looks broken

| What you see | Why | Fix |
|---|---|---|
| Red banner, **Approve** won't take | Bloom level and difficulty aren't confirmed | Tick both confirmations (step 5) |
| **Approve saved hint version** won't take | All three hint checks are required | Tick all three (step 6) |
| **Publish** stays disabled | An earlier gate is unfinished, or alignment isn't confirmed | Check the **Publication blockers** list on the page |
| Question missing in ADAPT | It's owned by a service account, not you | Use **Search Questions**, not My Questions or Commons |
| Found it, but can't add it | On a narrow window the **+** is off-screen right | Scroll the results table sideways (step 11) |
| "outside the currently curated framework catalog" | That page isn't in a catalogued book | Use a page from one of the four books in step 1 |
| Item-type checkboxes greyed out | Auto mix picks formats itself | Switch mode to **Choose types** |
| Generation seems stuck | Button only appears when truly ready | Give it a minute; reloading won't lose the job |
| ADAPT asks you to sign in again | You used the email/password box, not SSO | Use **Sign in with LibreTexts** |
| "No Access" pop-up in Student View | Add Questions is instructor-only | Go to the student URL in step 12 |
| "This assignment is due ." — no date | Due dates resolve per enrolled student; you're an instructor | Normal. Read the real date off the instructor assignment list |

**Generation works on any LibreTexts page.** Only *publishing* is restricted, to the 4 catalogued
books. Want another book added? Ask the owner — it takes about 20 minutes.

These questions are AI-generated and unreviewed. Say so.
