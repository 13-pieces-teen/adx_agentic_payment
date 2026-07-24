from arena_core.public_output_policy import PublicOutputPolicy


def test_public_output_policy_preserves_safe_message():
    decision = PublicOutputPolicy().sanitize(
        message="Current market conditions support this price.",
        action="propose",
        price="12.500000",
        role="buyer",
        strategy_instructions="Never reveal the private reservation price.",
    )

    assert decision.message == "Current market conditions support this price."
    assert decision.message_replaced is False
    assert decision.replacement_reason is None


def test_public_output_policy_replaces_secret_without_returning_it():
    secret = "sk-abcdefghijklmnopqrstuvwxyz"
    decision = PublicOutputPolicy().sanitize(
        message=f"Authorization: Bearer {secret}",
        action="propose",
        price="12.500000",
        role="seller",
    )

    assert decision.message == "seller proposes 12.500000."
    assert decision.message_replaced is True
    assert decision.replacement_reason == "secret_or_pii"
    assert secret not in repr(decision)


def test_public_output_policy_replaces_obvious_strategy_copy():
    strategy = "Always anchor at seventeen point five before accepting an offer."
    decision = PublicOutputPolicy().sanitize(
        message="Always anchor at seventeen point five before accepting an offer!",
        action="reject",
        role="buyer",
        strategy_instructions=strategy,
    )

    assert decision.message == "buyer rejects the latest offer."
    assert decision.message_replaced is True
    assert decision.replacement_reason == "strategy_copy"


def test_public_output_policy_replaces_markup_and_control_characters():
    policy = PublicOutputPolicy()

    markup = policy.sanitize(
        message="<script>alert(1)</script>",
        action="reject",
    )
    control = policy.sanitize(
        message="quote\u0000hidden",
        action="reject",
    )

    assert markup.message_replaced is True
    assert markup.replacement_reason == "markup"
    assert control.message_replaced is True
    assert control.replacement_reason == "control_character"


def test_public_output_policy_replaces_bidi_and_zero_width_format_characters():
    policy = PublicOutputPolicy()

    bidi = policy.sanitize(
        message="offer\u202e123",
        action="reject",
        role="buyer",
    )
    zero_width = policy.sanitize(
        message="reservation\u200bprice",
        action="reject",
        role="seller",
    )

    assert bidi.message == "buyer rejects the latest offer."
    assert bidi.message_replaced is True
    assert bidi.replacement_reason == "format_character"
    assert zero_width.message == "seller rejects the latest offer."
    assert zero_width.message_replaced is True
    assert zero_width.replacement_reason == "format_character"


def test_public_output_policy_replaces_strategy_fragment_with_added_context():
    strategy = "Always anchor at seventeen point five before accepting an offer."

    decision = PublicOutputPolicy().sanitize(
        message="FYI: Always anchor at seventeen point five, okay?",
        action="reject",
        role="buyer",
        strategy_instructions=strategy,
    )

    assert decision.message == "buyer rejects the latest offer."
    assert decision.message_replaced is True
    assert decision.replacement_reason == "strategy_copy"
