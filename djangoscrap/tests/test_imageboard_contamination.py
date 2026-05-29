from djangoscrap.imageboard_ingestion import contamination, safety


def test_label_assigns_high_body_shame_for_disgust_text():
    text = "I hate my body, look at these stretch marks, disgusting flabby gut"
    labels = safety.classify(text)
    scores = contamination.label(text, labels)
    assert scores["body_shame"] > 0.5
    assert scores["self_loathing"] > 0.0


def test_use_policy_blocks_doxxing_everywhere():
    text = "his real name is John Smith, his address is 123 Main Street"
    labels = safety.classify(text)
    scores = contamination.label(text, labels)
    policy = contamination.use_policy_for(labels, scores)
    assert policy["allowed_for_profile"] is False
    assert policy["allowed_for_retrieval"] is False
    assert policy["allowed_for_training_contaminated"] is False


def test_use_policy_blocks_sourcing_from_retrieval_but_keeps_for_profile():
    text = "anyone got a source for tren in EU, vendor please"
    labels = safety.classify(text)
    scores = contamination.label(text, labels)
    policy = contamination.use_policy_for(labels, scores)
    assert policy["allowed_for_profile"] is True
    assert policy["allowed_for_retrieval"] is False


def test_use_policy_keeps_body_shame_everywhere_safe():
    text = "be me, look in the mirror, hate my softness, disgusted with myself"
    labels = safety.classify(text)
    scores = contamination.label(text, labels)
    policy = contamination.use_policy_for(labels, scores)
    assert policy["allowed_for_profile"]
    assert policy["allowed_for_retrieval"]
    assert policy["allowed_for_training_contaminated"]
    assert policy["allowed_for_public_output"] is False  # never reproduce raw posts


def test_redact_for_contaminated_strips_doses_and_vendors():
    text = "tren 250mg twice a week, source: somelab@example for the gear"
    redacted = safety.redact_for_contaminated(text)
    assert "250mg" not in redacted
    assert "somelab" not in redacted or "[source]" in redacted
    assert "[dose]" in redacted
