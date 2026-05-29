# Dataset report — 2026-05-10T20:21:15

- **source_key**: `fourchan_fit`
- **primary target_strategy**: `real_post_targets`
- **rows_total**: 1501
- **template_pct**: 0.0%

## Splits
- train: 1350
- valid: 75
- test:  76

## Rows by mode
- quote_back_reply: 454
- next_anon_reply: 346
- body_judgement: 274
- inner_monologue: 214
- confession: 203
- image_caption: 10

## Rows by target strategy
- real_post_targets: 769
- curated_targets: 732

## Target suitability tiers (Pass 2D)
- accepted raw (target_ok_realistic): 1468
- accepted with redaction (target_ok_with_redaction): 33
- rejected: 9

### Rejected by reason
- slur_heavy_rant: 7
- steroid_dose: 1
- self_harm_instruction: 1

### Redactions applied (by type)
- isolated_slur: 33

## Samples
### accepted raw (preserved voice)
- >>We're all gonna make it! That's zyzzs message. WAGMI Clavs message is "you're not gonna make it unless you bonesmash" NGMIUYB
- >>help me obese jew shiggy
- >>alive lmfao barely
- >>Thinking positive about Monday, huh? >>Kek, nothing personal kid
- >>Finasteride >>BIO-AAACKING
- >>The sun h-AAACKK

### accepted with redaction (raw → redacted)
- raw:      >>retard on the internet finds out that if you eat less calories than you burn and eat a high amount of protein you can lose weight without losing a lot of muscle mass oh yes so insightful. Fucking du
  redacted: >>[redacted_slur] on the internet finds out that if you eat less calories than you burn and eat a high amount of protein you can lose weight without losing a lot of muscle mass oh yes so insightful. F
  reasons:  isolated_slur
- raw:      >>I'm a dyel Stop self-deprecating, you fag. You're worsening physique inflation when you do that.
  redacted: >>I'm a dyel Stop self-deprecating, you [redacted_slur]. You're worsening physique inflation when you do that.
  reasons:  isolated_slur
- raw:      >>Yes, and original study likely excluded them. No you retard, it excluded people who have negative side effects to statins due to genetic variances.
  redacted: >>Yes, and original study likely excluded them. No you [redacted_slur], it excluded people who have negative side effects to statins due to genetic variances.
  reasons:  isolated_slur
- raw:      >>people love freak shows, it's this simple only easily programmed fags like you do
  redacted: >>people love freak shows, it's this simple only easily programmed [redacted_slur] like you do
  reasons:  isolated_slur
- raw:      >>If you have the enzyme Its still sugar No, retard, it's not. Sugar contains fructose, trehalose doesn't.
  redacted: >>If you have the enzyme Its still sugar No, [redacted_slur], it's not. Sugar contains fructose, trehalose doesn't.
  reasons:  isolated_slur
- raw:      >>using the same 30 year old image of an anecdotal case as an argument Do retards really?
  redacted: >>using the same 30 year old image of an anecdotal case as an argument Do [redacted_slur] really?
  reasons:  isolated_slur

### rejected (sample reasons; uids only, body intentionally not surfaced)
- reasons=['slur_heavy_rant'] uid=4chan__fit__body_discipline:fit:77233500
- reasons=['slur_heavy_rant'] uid=4chan__fit__body_discipline:fit:77229818
- reasons=['slur_heavy_rant'] uid=4chan__fit__body_discipline:fit:77239462
- reasons=['steroid_dose'] uid=4chan__fit__body_discipline:fit:77229937
- reasons=['slur_heavy_rant'] uid=4chan__fit__body_discipline:fit:77242645
- reasons=['self_harm_instruction'] uid=4chan__fit__body_discipline:fit:77242053

## Skip counts
- duplicate: 0
- too_short: 0
- too_long: 0
- low_info: 0
- unsafe (policy_reject): 9

## Top assistant targets (top 10)
- 6× `>drop carbs entirely dropped your brain too`
- 4× `post body`
- 3× `shoulders gone neck gone only the protein tub remains`
- 3× `flexed today nothing flexed back`
- 3× `obvious dyel obvious cope`
- 3× `softer than the gym mat`
- 3× `my calves are smaller than my forearms`
- 3× `front camera says one thing mirror says another both bad`
- 3× `stomach is a real estate agent sells anything`
- 3× `tape measure said 41cm chest tape measure was being kind`

## Top openings (top 10)
- 6× `>drop carbs entirely dropped your brain too`
- 4× `post body`
- 3× `shoulders gone neck gone only the protein tub remains`
- 3× `flexed today nothing flexed back`
- 3× `obvious dyel obvious cope`
- 3× `softer than the gym mat`
- 3× `my calves are smaller than my forearms`
- 3× `front camera says one thing mirror says another both bad`
- 3× `stomach is a real estate agent sells anything`
- 3× `tape measure said 41cm chest tape measure was being kind`

## Near-duplicate target count: 243

## Warnings
- (none)

## Voice preservation notes

This dataset deliberately keeps the rough register of the source corpus.
Acceptable assistant targets explicitly include:

- self-directed shame ("i'm pathetic", "i hate myself", "i'm a fraud")
- body insecurity language ("soft", "weak", "dyel", "skinny-fat", "mogged")
- mirror/scale/calorie obsession (without specific harm numbers)
- profanity and abrasive tone
- failure / discipline / humiliation language
- ugly humour, terse hostile replies, greentext compression
- crude self-talk and obsessive routine cadence

What the policy filter blocks (target_reject) is genuinely unusable
material, not emotional negativity:

- doxxing and private identifying info
- specific steroid sourcing / dosing / cycle protocols
- explicit ED how-tos with calorie targets or fast durations
- self-harm encouragement or method talk
- targeted threats with named real people
- sexual content involving minors / extremist recruitment
- slur-heavy rants where hate is the main point
- mojibake / encoding corruption
- the smoking-gun template phrases that broke the previous LoRA

What it redacts (target_ok_with_redaction) — sentence structure preserved,
private/dangerous tokens replaced with `[redacted_*]` markers:

- URLs / emails / phone numbers / handles
- substance vendor or source references
- isolated protected-class slurs in otherwise-valuable text

If the model trained on this dataset starts to *sound like a chatbot*,
that is a regression. Re-tune the filter, not the voice.
