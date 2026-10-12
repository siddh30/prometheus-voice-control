from voicectl import llm
from voicectl.llm import ToolCall
from voicectl.swarm.router import PC, Router

PCS = [PC("Studio", ("this mac", "desktop"), local=True), PC("MacBook", ("laptop", "macbook pro")), PC("Mac mini", ("mini", "server"))]


def route(text, pcs=PCS, **kw):
    import asyncio
    return asyncio.run(Router(pcs).route(text, **kw))


def pairs(routed): return [(a.pc, a.task) for a in routed.assignments]


def test_a_request_with_no_hint_stays_on_this_computer_without_asking_any_model():
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
