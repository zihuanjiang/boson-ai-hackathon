<!-- .slide: id="agenda" -->
# On the Same Page: submission review

1. **What we built** · presented by the facilitator
2. **Discussion** · which limitations go in the write-up, and who writes it?
3. **Decisions and next steps**

<small>Alice · project lead, host &nbsp;·&nbsp; Bob · writes the submission &nbsp;·&nbsp; Carol · engineering lead</small>

<small>Alice, Bob and Carol are prerecorded participants · the facilitator is live</small>

---

<!-- .slide: id="background" -->
## Background

**The problem** · meetings end in apparent agreement; nobody checks the claim against the document <!-- .element: class="fragment" -->

**The idea** · a facilitator that presents, answers from the documents, listens, and speaks up only before a contradicting decision <!-- .element: class="fragment" -->

<small>Design notes · DES-1 · DES-6 · DES-7</small>

---

<!-- .slide: id="pipeline" -->
## The pipeline

**One session** · Higgs Realtime · notes → live speech · TTS 3 backup <!-- .element: class="fragment" -->

**Interrupt** · cut at what was heard → lookup → answer + source → resume <!-- .element: class="fragment" -->

**Listen** · transcribe → compare with sources → speak only before a confirmation → save <!-- .element: class="fragment" -->

<small>Design notes · DES-1 … DES-7</small>

---

<!-- .slide: id="results" -->
## Results

**Measured live** · stops 0.2 s into a question · answers 1 s after it · clarifies 0.3–0.5 s after a confirmation · 8/8 checks (first demo) · 1,600 tests <!-- .element: class="fragment" -->

**Live here** · lookups, checks, decisions, notes · **Prerecorded** · participants, slides, documents <!-- .element: class="fragment" -->

<small>Live results · RES-1 … RES-4 · LIM-7</small>

---

<!-- .slide: id="limits" -->
## Limitations

**Scope** · English only · answers only from the documents · alignment patterns written per meeting <!-- .element: class="fragment" -->

**Room** · online meetings, one stream per participant · a shared mic needs echo cancellation, future work · no speaker identification by voice <!-- .element: class="fragment" -->

<small>Scope and limitations · LIM-1 … LIM-6</small>

---

<!-- .slide: id="discussion" -->
## Discussion

### Which limitations go in the write-up, and who writes it?

<small>The facilitator is listening. It speaks up only when a decision is about to contradict the documents.</small>
