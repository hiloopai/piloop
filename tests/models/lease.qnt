// INVS: noOrphan,noOrphanProcess,noSpoolExpiry,noSilentStop,freedOnce,reportedOnce,triesBounded
// A worker's jobs, its spool and their leases: one worker, two leases. `make models` checks it.
// One action per function (paths under piloop/plugins/executor/):
//   acquire    kit/leases.py Leases.acquire + Leases.started; workers/worker.py Worker._turn
//   spool      worker.py Worker._report (the job ends; its thread goes)
//   send       worker.py Worker._send from Worker._turn; kit/leases.py Leases.report (core open)
//   giveUp     worker.py Worker._rejected: the job's crash report (Report.crashed) takes its place
//   refuse     kit/leases.py Leases.report or Leases.started on `Settled`, then Leases._end_settled
//   heartbeat  worker.py Worker._turn; kit/leases.py Leases.heartbeat
//   selfStop   worker.py Worker._guard_silence
//   expire     kit/leases.py Leases.expire + Leases._lose
//   restart    worker.py Worker.start (register) + Worker._recover; a job it cannot read is lost
//   settle     the environment: core closes the job's row elsewhere (a stop, a cancel, rows dropped at start)
// The server always answers (no partition), so any self-stop is a bug. It may reject any send of a report
// (a 5xx, a locked database), for ever. Time is in renew periods; each turn heartbeats first.
module lease {
  const FIX_CARRY: bool   // the heartbeat carries every held lease, spooled ones too
  const FIX_BUDGET: bool  // a loop call other than the heartbeat gives up within one renew
  const FIX_SETTLED: bool // a start or a report refused as settled ends the lease
  const FIX_GIVEUP: bool  // after TRIES rejected sends the job ends as a crash, and its slot is free
  const FIX_RECOVER: bool // a restarted worker that cannot read a job kills its process before it loses the lease
  pure val LEASES = Set(1, 2)
  pure val H = 16
  pure val TOL = 6        // toleration (300 s) / renew (10 s), scaled
  pure val SELF_STOP = 4  // toleration - 2 * renew
  pure val TRIES = 2      // report_tries (30), scaled
  pure val ENDED = Set("reported", "expired", "cancelled")

  // srv: queued | live | ENDED. wk: idle | running | spooled. proc: the job's process runs. ends: writes that end
  // the lease (each frees its slot). applied: reports core applied. rejects: rejected sends (saturates at TRIES).
  type L = { srv: str, exp: int, core: bool, wk: str, proc: bool, ends: int, applied: int, rejects: int }
  type S = { t: int, ls: int -> L, heard: int, busyTo: int,
    silentStop: bool, spoolExpired: bool, overTried: bool, op: str }
  var s: S

  pure def live(l: L): bool = l.srv == "live"
  pure def ended(l: L, st: str): L = { ...l, srv: st, ends: l.ends + 1 }
  pure def applied(l: L): L = { ...ended(l, "reported"), wk: "idle", core: false, applied: l.applied + 1 }
  def lease(i: int): L = s.ls.get(i)
  val turn = s.t >= s.busyTo and s.heard == s.t   // the loop is free and has heartbeated
  action write(i: int, l: L, op: str): bool = s' = { ...s, ls: s.ls.set(i, l), op: op }
  action init = s' = { t: 0, heard: 0, busyTo: 0, silentStop: false, spoolExpired: false, overTried: false, op: "init",
    ls: LEASES.mapBy(_ => { srv: "queued", exp: 0, core: true, wk: "idle", proc: false, ends: 0, applied: 0, rejects: 0 }) }

  action acquire(i: int): bool = all { turn, lease(i).srv == "queued",
    write(i, { ...lease(i), srv: "live", exp: s.t + TOL, wk: "running", proc: true }, "acquire") }
  action spool(i: int): bool = all { lease(i).wk == "running",
    write(i, { ...lease(i), wk: "spooled", proc: false }, "spool") }
  action report(i: int, ok: bool): bool = all { turn, lease(i).wk == "spooled", lease(i).core,
    not(FIX_GIVEUP) or lease(i).rejects < TRIES,
    val l = { ...lease(i), rejects: if (lease(i).rejects < TRIES) lease(i).rejects + 1 else TRIES }
    if (not(ok))  // rejected; without the budget, Remote._post resends it for self_stop
      s' = { ...s, ls: s.ls.set(i, l), busyTo: s.t + (if (FIX_BUDGET) 1 else SELF_STOP + 1),
        overTried: s.overTried or lease(i).rejects >= TRIES, op: "report_rejected" }
    else all { live(lease(i)), write(i, applied(lease(i)), "report") } }
  action send(i: int): bool = any { report(i, true), report(i, false) }
  action giveUp(i: int): bool = all { FIX_GIVEUP, turn, lease(i).wk == "spooled", lease(i).rejects >= TRIES,
    live(lease(i)), write(i, applied(lease(i)), "giveUp") }
  action refuse(i: int): bool = all { turn, lease(i).wk != "idle", not(lease(i).core), live(lease(i)),
    val dropped = { ...lease(i), wk: "idle", proc: false }   // the worker stops the job and forgets it either way
    write(i, if (FIX_SETTLED) ended(dropped, "cancelled") else dropped, "refuse") }
  action heartbeat = all { s.t >= s.busyTo, s.heard < s.t or s.t == 0,
    s' = { ...s, heard: s.t, op: "heartbeat", ls: s.ls.keys().mapBy(i =>
      val l = lease(i)
      if (live(l) and (l.wk == "running" or (FIX_CARRY and l.wk == "spooled"))) { ...l, exp: s.t + TOL } else l) } }
  action selfStop = all { s.t - s.heard > SELF_STOP, LEASES.exists(i => lease(i).wk == "running"),
    s' = { ...s, silentStop: true, op: "selfStop", ls: s.ls.keys().mapBy(i =>
      if (lease(i).wk == "running") { ...lease(i), wk: "spooled", proc: false } else lease(i)) } }
  action expire(i: int): bool = all { live(lease(i)), lease(i).exp <= s.t,
    s' = { ...s, op: "expire", spoolExpired: s.spoolExpired or lease(i).wk == "spooled",
      ls: s.ls.set(i, { ...ended(lease(i), "expired"), core: false }) } }
  // Threads go and the spool stays. A running job that it reads again goes on; one that it cannot read is lost.
  action restart(unreadable: bool): bool = all { LEASES.exists(i => lease(i).wk != "idle"),
    s' = { ...s, busyTo: s.t, op: "restart", ls: s.ls.keys().mapBy(i =>
      val l = lease(i)
      if (l.wk != "running" or not(unreadable)) l
      else if (live(l)) { ...ended(l, "expired"), wk: "idle", core: false, proc: l.proc and not(FIX_RECOVER) }
      else { ...l, wk: "idle", core: false, proc: l.proc and not(FIX_RECOVER) }) } }
  action settle(i: int): bool = all { lease(i).core, lease(i).srv != "queued",
    write(i, { ...lease(i), core: false }, "settle") }
  // Time moves only once the free loop has heartbeated: the loop turns every renew period.
  action tick = all { s.t < H, s.t < s.busyTo or s.heard == s.t, s' = { ...s, t: s.t + 1, op: "tick" } }

  action step = any {
    nondet i = LEASES.oneOf()
    any { acquire(i), spool(i), send(i), giveUp(i), refuse(i), expire(i), settle(i) },
    nondet unreadable = Bool.oneOf()
    restart(unreadable),
    heartbeat, selfStop, tick }

  // A live lease always has a holder that renews or reports it.
  val noOrphan = LEASES.forall(i => live(lease(i)) implies lease(i).wk != "idle")
  // A job's process runs only on a live lease that its worker runs: nothing runs that nothing tracks.
  val noOrphanProcess = LEASES.forall(i => lease(i).proc implies (live(lease(i)) and lease(i).wk == "running"))
  // A lease whose report sits in the spool of a heartbeating worker never expires.
  val noSpoolExpiry = not(s.spoolExpired)
  // A worker that the server answers never stops its own jobs.
  val noSilentStop = not(s.silentStop)
  // A slot is freed exactly once.
  val freedOnce = LEASES.forall(i => lease(i).ends <= 1 and (ENDED.contains(lease(i).srv) iff lease(i).ends == 1))
  val reportedOnce = LEASES.forall(i => lease(i).applied <= 1)
  // No report is sent again after TRIES rejections: the bound that liveness rests on.
  val triesBounded = not(s.overTried)
  val inv = noOrphan and noOrphanProcess and noSpoolExpiry and noSilentStop and freedOnce and reportedOnce
    and triesBounded

  // Liveness: a spooled report's lease ends, even if the server rejects every report.
  temporal fair = LEASES.forall(i => weakFair(giveUp(i), s) and weakFair(send(i), s))
    and weakFair(heartbeat, s) and weakFair(tick, s)
  temporal settles = fair implies LEASES.forall(i => always(lease(i).wk == "spooled" implies eventually(not(live(lease(i))))))
}

// `fixed` is the design of the code. `before` is main before #739's lease PR; each `no_*` drops one fix.
module fixed { import lease(FIX_CARRY = true, FIX_BUDGET = true, FIX_SETTLED = true, FIX_GIVEUP = true, FIX_RECOVER = true).* }
module before { import lease(FIX_CARRY = false, FIX_BUDGET = false, FIX_SETTLED = false, FIX_GIVEUP = false, FIX_RECOVER = false).* }
module no_carry { import lease(FIX_CARRY = false, FIX_BUDGET = true, FIX_SETTLED = true, FIX_GIVEUP = true, FIX_RECOVER = true).* }
module no_budget { import lease(FIX_CARRY = true, FIX_BUDGET = false, FIX_SETTLED = true, FIX_GIVEUP = true, FIX_RECOVER = true).* }
module no_settled { import lease(FIX_CARRY = true, FIX_BUDGET = true, FIX_SETTLED = false, FIX_GIVEUP = true, FIX_RECOVER = true).* }
module no_giveup { import lease(FIX_CARRY = true, FIX_BUDGET = true, FIX_SETTLED = true, FIX_GIVEUP = false, FIX_RECOVER = true).* }
module no_recover { import lease(FIX_CARRY = true, FIX_BUDGET = true, FIX_SETTLED = true, FIX_GIVEUP = true, FIX_RECOVER = false).* }

// Each fix's bug, as a pinned trace: it runs to the violation in the design without the fix (`quint test`). The
// simulator alone misses some of them, such as the self-stop.
module scenarios {
  import lease(FIX_CARRY = false, FIX_BUDGET = false, FIX_SETTLED = false, FIX_GIVEUP = false, FIX_RECOVER = false).*
  // B1: a rejected report holds the loop past self_stop, and the worker kills its other job.
  run selfStopTest = init.then(tick).then(heartbeat).then(acquire(1)).then(acquire(2)).then(spool(1))
    .then(report(1, false)).then(5.reps(_ => tick)).then(selfStop).then(all { assert(not(noSilentStop)), s' = s })
  // B2: the heartbeat does not carry a spooled lease, which expires while its worker heartbeats.
  run spoolExpiryTest = init.then(tick).then(heartbeat).then(acquire(1)).then(spool(1))
    .then(6.reps(_ => tick.then(heartbeat))).then(expire(1)).then(all { assert(not(noSpoolExpiry)), s' = s })
  // B3: a report refused as settled leaves its lease live with no holder.
  run settledTest = init.then(tick).then(heartbeat).then(acquire(1)).then(spool(1)).then(settle(1)).then(refuse(1))
    .then(all { assert(not(noOrphan)), s' = s })
  // #570: a restarted worker that cannot read its job loses the lease and leaves the process running.
  run recoverTest = init.then(tick).then(heartbeat).then(acquire(1)).then(restart(true))
    .then(all { assert(not(noOrphanProcess)), s' = s })
}
module giveup_scenario {
  import lease(FIX_CARRY = true, FIX_BUDGET = true, FIX_SETTLED = true, FIX_GIVEUP = true, FIX_RECOVER = true).*
  // A report rejected TRIES times gives way to the crash report: the lease ends once, and nothing is left.
  run giveUpTest = init.then(tick).then(heartbeat).then(acquire(1)).then(spool(1)).then(report(1, false))
    .then(tick).then(heartbeat).then(report(1, false)).then(tick).then(heartbeat).then(giveUp(1))
    .then(all { assert(lease(1).srv == "reported" and lease(1).ends == 1 and inv), s' = s })
}
