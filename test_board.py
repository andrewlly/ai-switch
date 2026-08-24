#!/usr/bin/env python3
"""Tests for the ccfleet task board.

No agents, no tmux, no clock: `wait_claim` takes its sleep and its clock as
arguments so the waiting is exercised without any. Run directly:

    python3 ~/.local/share/cc-accounts/test_board.py
"""

import importlib.util
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("board", HERE / "board.py")
board = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(board)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="board-case-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bd = board.Board(self.tmp / "boards" / "fleet.json")
        self.bd.init("do the thing", auto_review=False)

    def review_board(self):
        bd = board.Board(self.tmp / "boards" / "reviewed.json")
        bd.init("do the thing", auto_review=True)
        return bd

    def states(self, bd=None):
        return {t["id"]: t["state"] for t in (bd or self.bd).read()["tasks"]}


class TestRole(unittest.TestCase):
    def test_a_member_carries_its_role_in_its_name(self):
        self.assertEqual(board.role_of("worker-db"), "worker")
        self.assertEqual(board.role_of("checker-review"), "checker")

    def test_anything_unrecognised_is_a_worker_not_an_error(self):
        for name in ("orch", "db", "", "reviewer"):
            self.assertEqual(board.role_of(name), "worker")


class TestClaiming(Base):
    def test_a_claim_hands_out_one_task_and_marks_who_has_it(self):
        self.bd.add("first")
        code, task = self.bd.claim("worker-db")
        self.assertEqual(code, board.GOT_TASK)
        self.assertEqual((task["id"], task["owner"], task["attempts"]),
                         ("t1", "worker-db", 1))
        self.assertEqual(self.states(), {"t1": "claimed"})

    def test_a_claimed_task_is_not_handed_out_twice(self):
        self.bd.add("only one")
        self.bd.claim("worker-a")
        code, task = self.bd.claim("worker-b")
        self.assertEqual(code, board.DRAINED if False else board.NO_TASK_YET)
        self.assertIsNone(task)

    def test_roles_cannot_take_each_others_work(self):
        self.bd.add("build it", role="worker")
        self.bd.add("check it", role="checker")
        self.assertEqual(self.bd.claim("checker-review")[1]["title"], "check it")
        self.assertEqual(self.bd.claim("worker-db")[1]["title"], "build it")

    def test_a_dependency_holds_a_task_back_until_it_settles(self):
        self.bd.add("first")
        self.bd.add("second", deps=["t1"])
        self.bd.claim("worker-a")                       # takes t1
        self.assertEqual(self.bd.claim("worker-b")[0], board.NO_TASK_YET)
        self.bd.done("t1")
        self.assertEqual(self.bd.claim("worker-b")[1]["id"], "t2")

    def test_an_accepted_dependency_also_settles_it(self):
        self.bd.add("first")
        self.bd.add("second", deps=["t1"])
        self.bd.claim("worker-a")
        self.bd.done("t1")
        self.bd.accept("t1")
        self.assertEqual(self.bd.claim("worker-b")[1]["id"], "t2")

    def test_blocking_a_task_blocks_what_was_waiting_on_it(self):
        """Otherwise the stranded task stays `pending` forever, the board never
        drains, and every member spins on "nothing yet" until someone looks."""
        self.bd.add("first")
        self.bd.add("second", deps=["t1"])
        self.bd.claim("worker-a")
        self.bd.block("t1", "needs a decision")
        self.assertEqual(self.states(), {"t1": "blocked", "t2": "blocked"})
        self.assertIn("depends on t1", self.bd.read()["tasks"][1]["notes"][0])
        self.assertEqual(self.bd.claim("worker-b")[0], board.DRAINED)

    def test_a_task_someone_is_working_on_is_not_cascaded(self):
        """It is in a member's hands and will report; only work that can never
        be claimed has to be killed to keep the board from hanging."""
        self.bd.add("first")
        self.bd.add("second", deps=["t1"])
        self.bd.claim("worker-a")                       # t1
        self.bd.done("t1")
        self.bd.claim("worker-b")                       # t2, now in flight
        self.bd.block("t1", "turned out wrong")
        self.assertEqual(self.states()["t2"], "claimed")

    def test_the_cascade_follows_the_whole_chain(self):
        self.bd.add("first")
        self.bd.add("second", deps=["t1"])
        self.bd.add("third", deps=["t2"])
        self.bd.add("unrelated")
        self.bd.block("t1", "needs a decision")
        self.assertEqual(self.states(), {"t1": "blocked", "t2": "blocked",
                                         "t3": "blocked", "t4": "pending"})

    def test_the_cascade_leaves_finished_work_alone(self):
        self.bd.add("first")
        self.bd.add("second", deps=["t1"])
        self.bd.claim("worker-a")
        self.bd.done("t1")
        self.bd.claim("worker-b")
        self.bd.done("t2")
        self.bd.block("t1", "turned out wrong")
        self.assertEqual(self.states()["t2"], "done")


class TestDrained(Base):
    def test_an_empty_board_is_drained(self):
        self.assertEqual(self.bd.claim("worker-db"), (board.DRAINED, None))

    def test_someone_still_working_means_not_drained(self):
        """The distinction the whole loop rests on: 4 means wait, 5 means stop,
        and a member that stops while another is still working never comes back
        for the work that member is about to unblock."""
        self.bd.add("first")
        self.bd.add("second", deps=["t1"])
        self.bd.claim("worker-a")
        self.assertEqual(self.bd.claim("worker-b")[0], board.NO_TASK_YET)

    def test_a_task_waiting_on_review_means_not_drained(self):
        bd = self.review_board()
        bd.add("first")
        bd.claim("worker-a")
        bd.done("t1")
        self.assertEqual(bd.claim("worker-b")[0], board.NO_TASK_YET)

    def test_everything_terminal_is_drained(self):
        self.bd.add("first")
        self.bd.claim("worker-a")
        self.bd.done("t1")
        self.bd.accept("t1")
        self.assertEqual(self.bd.claim("worker-a")[0], board.DRAINED)


class TestReview(Base):
    def test_a_worker_finishing_does_not_finish_the_task(self):
        bd = self.review_board()
        bd.add("build it", files="a.py")
        bd.claim("worker-db")
        task = bd.done("t1", "worker-db", "built it")
        self.assertEqual(task["state"], "review")
        review = bd.read()["tasks"][1]
        self.assertEqual((review["role"], review["reviews"], review["state"]),
                         ("checker", "t1", "pending"))
        self.assertEqual(review["files"], "a.py")

    def test_without_a_checker_done_means_done(self):
        self.bd.add("build it")
        self.bd.claim("worker-db")
        self.assertEqual(self.bd.done("t1")["state"], "done")
        self.assertEqual(len(self.bd.read()["tasks"]), 1)

    def test_a_rejection_reopens_the_work_carrying_the_reason(self):
        bd = self.review_board()
        bd.add("build it")
        bd.claim("worker-db")
        bd.done("t1", "worker-db", "built it")
        bd.claim("checker-review")
        bd.reject("t1", "no test")
        task = bd.read()["tasks"][0]
        self.assertEqual((task["state"], task["owner"]), ("pending", None))
        self.assertIn("rejected: no test", task["notes"])
        self.assertEqual(self.states(bd)["t2"], "done")   # that review is spent

    def test_a_reopened_task_is_claimable_by_anyone_and_counts_the_attempt(self):
        bd = self.review_board()
        bd.add("build it")
        bd.claim("worker-a")
        bd.done("t1", "worker-a")
        bd.reject("t1", "no test")
        code, task = bd.claim("worker-b")
        self.assertEqual(code, board.GOT_TASK)
        self.assertEqual((task["id"], task["attempts"], task["owner"]),
                         ("t1", 2, "worker-b"))

    def test_accepting_closes_the_review_that_was_still_open(self):
        bd = self.review_board()
        bd.add("build it")
        bd.claim("worker-db")
        bd.done("t1", "worker-db")
        bd.accept("t1", "fine")
        self.assertEqual(self.states(bd), {"t1": "accepted", "t2": "done"})

    def test_every_attempt_gets_its_own_review(self):
        bd = self.review_board()
        bd.add("build it")
        bd.claim("worker-a"); bd.done("t1", "worker-a"); bd.reject("t1", "again")
        bd.claim("worker-a"); bd.done("t1", "worker-a")
        reviews = [t for t in bd.read()["tasks"] if t.get("reviews") == "t1"]
        self.assertEqual([t["state"] for t in reviews], ["done", "pending"])

    def test_a_checkers_own_task_is_never_sent_for_review(self):
        bd = self.review_board()
        bd.add("check it", role="checker")
        bd.claim("checker-review")
        self.assertEqual(bd.done("t1")["state"], "done")
        self.assertEqual(len(bd.read()["tasks"]), 1)

    def test_an_unknown_task_is_an_error_not_a_silent_no_op(self):
        for verb, args in (("done", ("t9",)), ("accept", ("t9",)),
                           ("reject", ("t9", "x")), ("block", ("t9", "x"))):
            with self.subTest(verb=verb), self.assertRaises(KeyError):
                getattr(self.bd, verb)(*args)


class TestWaiting(Base):
    """`next` blocks, which is what removes the need for a dispatcher."""

    def fake_clock(self):
        now = [0.0]
        return now, (lambda: now[0]), (lambda s: now.__setitem__(0, now[0] + s))

    def test_it_returns_the_moment_work_appears(self):
        now, clock, sleep = self.fake_clock()
        self.bd.add("first")
        code, task = self.bd.wait_claim("worker-db", 240, sleep=sleep, clock=clock)
        self.assertEqual((code, task["id"]), (board.GOT_TASK, "t1"))
        self.assertEqual(now[0], 0.0)                  # never slept

    def test_it_gives_up_with_retry_me_not_with_stop(self):
        self.bd.add("first")
        self.bd.add("second", deps=["t1"])
        self.bd.claim("worker-a")
        now, clock, sleep = self.fake_clock()
        code, task = self.bd.wait_claim("worker-b", 10, sleep=sleep, clock=clock)
        self.assertEqual((code, task), (board.NO_TASK_YET, None))
        self.assertGreaterEqual(now[0], 10)

    def test_it_stops_at_once_when_the_board_is_drained(self):
        now, clock, sleep = self.fake_clock()
        code, _ = self.bd.wait_claim("worker-db", 600, sleep=sleep, clock=clock)
        self.assertEqual(code, board.DRAINED)
        self.assertEqual(now[0], 0.0)

    def test_it_picks_up_work_that_arrives_while_it_waits(self):
        now, clock, sleep = self.fake_clock()
        self.bd.add("first")
        self.bd.add("second", deps=["t1"])
        self.bd.claim("worker-a")

        def sleep_and_finish(seconds):
            sleep(seconds)
            if now[0] >= 4:
                self.bd.done("t1")
        code, task = self.bd.wait_claim("worker-b", 60, sleep=sleep_and_finish,
                                        clock=clock)
        self.assertEqual((code, task["id"]), (board.GOT_TASK, "t2"))
        self.assertLess(now[0], 60)


class TestConcurrency(Base):
    def test_simultaneous_claims_never_hand_out_the_same_task(self):
        """The reason every mutation takes a lock: two members reaching `next`
        in the same instant is the normal case, not the edge case."""
        for n in range(20):
            self.bd.add(f"task {n}")
        got, errors = [], []
        lock = threading.Lock()

        def grab(name):
            try:
                own = board.Board(self.bd.path)      # its own file handles
                while True:
                    code, task = own.claim(name)
                    if code != board.GOT_TASK:
                        return
                    with lock:
                        got.append(task["id"])
            except Exception as exc:                 # noqa: BLE001 - reported
                errors.append(exc)

        threads = [threading.Thread(target=grab, args=(f"worker-{i}",))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(got), 20)
        self.assertEqual(len(set(got)), 20, "a task was handed out twice")

    def test_the_file_is_never_left_half_written(self):
        for n in range(10):
            self.bd.add(f"task {n}")

        def churn(name):
            own = board.Board(self.bd.path)
            for _ in range(20):
                own.read()
                own.add(f"from {name}")

        threads = [threading.Thread(target=churn, args=(f"w{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        data = self.bd.read()                       # parses, or this raises
        self.assertEqual(len(data["tasks"]), 10 + 80)
        self.assertEqual(len({t["id"] for t in data["tasks"]}), 90)


class TestSummary(Base):
    def test_it_counts_by_state_and_reports_the_goal(self):
        self.bd.add("a"); self.bd.add("b")
        self.bd.claim("worker-db")
        summary = self.bd.summary()
        self.assertEqual(summary["goal"], "do the thing")
        self.assertEqual(summary["counts"]["pending"], 1)
        self.assertEqual(summary["counts"]["claimed"], 1)
        self.assertFalse(summary["drained"])

    def test_a_missing_board_reads_as_empty_rather_than_raising(self):
        missing = board.Board(self.tmp / "nope" / "gone.json")
        self.assertFalse(missing.exists())
        self.assertEqual(missing.summary()["tasks"], [])
        self.assertTrue(missing.summary()["drained"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
