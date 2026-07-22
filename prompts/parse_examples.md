# Parsing examples (few-shot)

Below are three anonymized placeholder examples. The companies and names are
fictional, and there are no specific amounts or details. A user can replace
these examples with their own real notes to improve accuracy.

## Example 1 — status update

Directory of active items:

```json
[{"id": 4, "title": "Vendor selection for Acme Corp", "company": "Acme Corp", "partner": "Alex", "stage": "In Progress"}]
```

Note to process:

```
for Acme Corp we approved the vendor Panda Limited, checking payment alignment with the reviewers
```

Expected response:

```json
{"suggested_deal_id": 4, "note_type": "status", "confidence": 0.9, "draft_text": "Vendor Panda Limited passed review, checking payment alignment with the reviewers."}
```

## Example 2 — task with no clear item

Directory of active items:

```json
[{"id": 3, "title": "Selecting a new partner for Orionis", "company": "Orionis", "partner": "Sam", "stage": "To Do"}]
```

Note to process:

```
figure out what to do with the payments still owed to partners. For some of them we already sent funds.
```

Expected response:

```json
{"suggested_deal_id": null, "note_type": "task", "confidence": 0.3, "draft_text": "Figure out the outstanding partner payments; funds were already sent for some of them."}
```

## Example 3 — reminder with an arrangement

Directory of active items:

```json
[{"id": 7, "title": "Move to software development Globex", "company": "Globex", "partner": "Sam", "stage": "Review"}]
```

Note to process:

```
on Thursday there is a meeting about the software payment rollout
```

Expected response:

```json
{"suggested_deal_id": 7, "note_type": "reminder", "confidence": 0.75, "draft_text": "A meeting is scheduled on Thursday to discuss the software payment rollout."}
```
