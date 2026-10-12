from voicectl import llm
from voicectl.llm import ToolCall
from voicectl.swarm.router import PC, Router

PCS = [PC("Studio", ("this mac", "desktop"), local=True), PC("MacBook", ("laptop", "macbook pro")), PC("Mac mini", ("mini", "server"))]


def route(text, pcs=PCS, **kw):
    import asyncio
    return asyncio.run(Router(pcs).route(text, **kw))


def pairs(routed): return [(a.pc, a.task) for a in routed.assignments]


def test_a_single_job_with_no_hint_stays_on_this_computer_without_asking_any_model(monkeypatch):
    monkeypatch.setattr(llm, "current", lambda: (_ for _ in ()).throw(AssertionError("no model call expected")))
    r = route("open linkedin")
    assert pairs(r) == [("Studio", "open linkedin")] and r.how == "default"


def test_a_compound_job_with_no_hint_falls_back_to_this_computer_when_the_model_is_unavailable(monkeypatch):
    monkeypatch.setattr(llm, "current", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    r = route("open linkedin and search for product manager jobs")
    assert pairs(r) == [("Studio", "open linkedin and search for product manager jobs")] and r.how == "default"


def test_a_named_computer_gets_the_task_without_the_routing_words():
    assert pairs(route("search for jobs on the MacBook")) == [("MacBook", "search for jobs")]
    assert pairs(route("open gmail on my mac mini")) == [("Mac mini", "open gmail")]
    assert pairs(route("play lofi on the mini")) == [("Mac mini", "play lofi")]                    # aliases work
    assert pairs(route("check the news on my laptop please")) == [("MacBook", "check the news please")]
    assert route("open gmail on the macbook").how == "named"


def test_two_computers_two_tasks_in_one_sentence():
    r = route("open gmail on the MacBook and play music on the mac mini")
    assert pairs(r) == [("MacBook", "open gmail"), ("Mac mini", "play music")]


def test_one_task_for_two_named_computers_goes_to_both():
    r = route("download the invoice on the MacBook and the mac mini")
    assert sorted(a.pc for a in r.assignments) == ["Mac mini", "MacBook"] and {a.task for a in r.assignments} == {"download the invoice"}


def test_both_and_all_reach_every_online_computer():
    r = route("check my calendar on both")
    assert r.how == "all" and sorted(a.pc for a in r.assignments) == ["Mac mini", "MacBook", "Studio"] and {a.task for a in r.assignments} == {"check my calendar"}
    assert route("update chrome on all my computers").how == "all"
    assert route("sync everything everywhere").how == "all"
    r2 = route("open the dashboard on all of them", online={"Studio", "MacBook"})              # the mini is offline: it is skipped, not failed
    assert sorted(a.pc for a in r2.assignments) == ["MacBook", "Studio"]


def test_the_other_computer_is_understood_when_there_is_only_one():
    two = [PC("Studio", local=True), PC("MacBook", ("laptop",))]
    r = route("open gmail on the other computer", pcs=two)
    assert pairs(r) == [("MacBook", "open gmail")] and r.how == "other"
    assert pairs(route("play it on my other mac", pcs=two)) == [("MacBook", "play it")]


def test_a_computer_picked_on_the_widget_wins_over_everything():
    r = route("open gmail on the macbook", picked="Mac mini")
    assert pairs(r) == [("Mac mini", "open gmail on the macbook")] and r.how == "picked"
    assert pairs(route("open gmail", picked="auto")) == [("Studio", "open gmail")]               # "auto" is not a computer


def test_with_three_computers_the_other_one_is_ambiguous_so_the_model_splits_it_and_mistakes_are_ignored():
    class Fake:
        def tool_calls(self, **kw):
            return [ToolCall("assign", {"assignments": [{"pc": "MacBook", "task": "check mail"}, {"pc": "Toaster", "task": "x"},
                                                        {"pc": "mini", "task": "play music"}, {"pc": "Studio", "task": "  "}]})]
    llm.set_llm(Fake())
    r = route("do the mail on the other computer and the music somewhere else")
    assert r.how == "llm" and pairs(r) == [("MacBook", "check mail"), ("Mac mini", "play music")]     # unknown computer and empty task dropped


def test_if_the_model_is_unavailable_the_request_stays_here():
    class Broken:
        def tool_calls(self, **kw): raise RuntimeError("no api key")
    llm.set_llm(Broken())
    r = route("do the mail on the other computer")
    assert pairs(r) == [("Studio", "do the mail on the other computer")] and r.how == "default"


def test_this_mac_and_here_point_at_the_computer_you_are_speaking_on():
    assert pairs(route("open gmail on this mac and play music on the mini")) == [("Studio", "open gmail"), ("Mac mini", "play music")]
    assert pairs(route("close all tabs here")) == [("Studio", "close all tabs")]


def test_of_for_and_a_leading_name_also_name_a_computer():
    assert pairs(route("close all tabs of the MacBook")) == [("MacBook", "close all tabs")]
    assert pairs(route("MacBook, open my profile")) == [("MacBook", "open my profile")]


def test_the_word_both_in_a_sentence_is_not_a_request_for_both_computers():
    assert route("I think you can control both, try again").how != "all"
    assert route("check the weather on both").how == "all"
    assert route("open gmail on both computers").how == "all"


def test_a_split_request_or_a_half_named_one_is_divided_by_the_model_not_guessed(monkeypatch):
    class Fake:
        def tool_calls(self, **kw):
            return [ToolCall("assign", {"assignments": [{"pc": "Studio", "task": "open obvious on youtube"}, {"pc": "MacBook", "task": "open my profile on linkedin"}]})]
    monkeypatch.setattr(llm, "current", lambda: Fake())
    expect = [("Studio", "open obvious on youtube"), ("MacBook", "open my profile on linkedin")]
    assert pairs(route("open my profile on linkedin and obvious on youtube on different computers")) == expect
    assert pairs(route("open obvious on youtube and open my profile on linkedin on the macbook")) == expect


def _fake(assignments, monkeypatch):
    class Fake:
        def tool_calls(self, **kw):
            return [ToolCall("assign", {"assignments": assignments})]
    monkeypatch.setattr(llm, "current", lambda: Fake())


def test_independent_jobs_with_no_computer_named_are_spread_by_the_model(monkeypatch):
    _fake([{"pc": "Studio", "task": "play lofi on youtube"}, {"pc": "MacBook", "task": "open gmail"}], monkeypatch)
    r = route("play lofi on youtube and open gmail")
    assert r.how == "split" and pairs(r) == [("Studio", "play lofi on youtube"), ("MacBook", "open gmail")]


def test_two_computers_never_get_the_same_job_unless_you_said_both(monkeypatch):
    _fake([{"pc": "Studio", "task": "open gmail"}, {"pc": "MacBook", "task": "Open Gmail"}, {"pc": "Mac mini", "task": "play music"}], monkeypatch)
    r = route("open gmail and play music")
    assert pairs(r) == [("Studio", "open gmail"), ("Mac mini", "play music")]
    assert len(route("open gmail on both").assignments) == 3                   # an explicit "both" still means both
