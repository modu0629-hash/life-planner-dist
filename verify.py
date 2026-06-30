"""3벌 파싱 결과 합의 — 순수 함수(claude/Flask 의존 없음)."""
from collections import Counter, defaultdict


def verify_consensus(lists):
    n_runs = len(lists)  # 보통 3
    by_date = defaultdict(list)          # date -> [(time, candidate), ...] (벌 순서 유지)
    for lst in lists:
        for c in (lst or []):
            d = c.get("start_date")
            if not d:
                continue
            by_date[d].append((c.get("start_time") or "", c))

    out = []
    for date, entries in by_date.items():
        tcount = Counter(t for t, _ in entries)
        first_by_time = {}
        for t, c in entries:
            first_by_time.setdefault(t, c)

        majors = [t for t, n in tcount.items() if n >= 2]
        minors = [(t, n) for t, n in tcount.items() if n == 1]

        if majors:
            # All majors unanimous if all appear exactly n_runs times
            all_majors_unanimous = all(tcount[m] == n_runs for m in majors)
            for t in majors:
                rep = dict(first_by_time[t])
                n = tcount[t]
                clean = (not minors and all_majors_unanimous)
                if clean:
                    rep["confidence"] = "certain"
                    rep["verify_note"] = ""
                else:
                    rep["confidence"] = "ambiguous"
                    if minors:
                        parts = ["%s(%d)" % (t or "시간없음", n)]
                        parts += ["%s(%d)" % (mt or "시간없음", mn) for mt, mn in minors]
                        rep["verify_note"] = "시간: " + " vs ".join(parts)
                    elif n == n_runs:
                        rep["verify_note"] = "같은 날 다른 시간과 갈림"
                    else:
                        rep["verify_note"] = "3벌 중 %d벌만 일치" % n
                out.append(rep)
        else:
            # major 없음(전부 1회씩) — r1(첫 등장) 채택
            rep = dict(entries[0][1])
            rep["confidence"] = "ambiguous"
            if len(tcount) == 1:
                rep["verify_note"] = "1벌에서만 발견"
            else:
                times = ", ".join((t or "시간없음") for t in tcount)
                rep["verify_note"] = "3벌이 제각각: " + times
            out.append(rep)
    return out
