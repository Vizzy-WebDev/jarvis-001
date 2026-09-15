from jarvis.model_system.capabilities import (
    Capabilities, Support, capabilities_from_dict, merge_capabilities,
)


def test_unknown_is_the_default_for_everything():
    caps = Capabilities()
    assert caps.tool_calling is Support.UNKNOWN
    assert caps.vision is Support.UNKNOWN
    assert caps.get("tool_calling") is Support.UNKNOWN
    assert caps.get("no_such_capability") is Support.UNKNOWN


def test_get_never_raises_on_a_bad_name():
    assert Capabilities().get("") is Support.UNKNOWN
    assert Capabilities().get(None) is Support.UNKNOWN


def test_get_normalises_camel_case():
    caps = Capabilities(web_search=Support.YES)
    assert caps.get("webSearch") is Support.YES
    assert caps.get("web_search") is Support.YES


def test_get_accepts_the_older_short_names():
    caps = Capabilities(video_input=Support.YES, audio_input=Support.NO, vision=Support.YES)
    assert caps.get("video") is Support.YES
    assert caps.get("audio") is Support.NO
    assert caps.get("image") is Support.YES


def test_capabilities_from_dict_reads_booleans_and_strings():
    caps = capabilities_from_dict({"tool_calling": True, "web_search": "no", "vision": "yes"})
    assert caps.tool_calling is Support.YES
    assert caps.web_search is Support.NO
    assert caps.vision is Support.YES


def test_capabilities_from_dict_never_raises_on_garbage():
    caps = capabilities_from_dict({"tool_calling": object(), "vision": []})
    assert caps.tool_calling is Support.UNKNOWN
    assert caps.vision is Support.UNKNOWN


def test_merge_is_per_field_not_per_object():
    """A discovered answer for one field must never blank a catalog answer for
    another sitting beside it."""
    user = {}
    discovered = {"vision": True}
    catalog = Capabilities(tool_calling=Support.YES, web_search=Support.NO)
    merged = merge_capabilities(user, discovered, catalog)
    assert merged.vision is Support.YES        # from discovered
    assert merged.tool_calling is Support.YES  # from catalog, not erased
    assert merged.web_search is Support.NO


def test_merge_precedence_is_first_source_wins():
    user = {"tool_calling": False}
    discovered = {"tool_calling": True}
    merged = merge_capabilities(user, discovered)
    assert merged.tool_calling is Support.NO


def test_merge_with_no_sources_is_all_unknown():
    merged = merge_capabilities()
    assert merged.tool_calling is Support.UNKNOWN
    assert merged.vision is Support.UNKNOWN
