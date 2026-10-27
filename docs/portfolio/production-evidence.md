# Production Evidence (Redacted)

Archive date: 2026-10-27

This summary retains the measurable production observations while excluding manuscript text, story titles, character names, provider responses, and local run artifacts.

## Complete 12-Chapter Run

- Output: 12 chapters, approximately 30,474 English words, average approximately 2,539 words per chapter.
- Editorial review recorded a chapter-ending hook in 10 of 12 chapters.
- The full-book assessment recorded 19 issue groups and approximately 45 text-level issues, including continuity conflicts, pacing repetition, unresolved threads, and formatting defects.
- Assessment: complete and readable sample; not approved for direct publication.

## Additional Local Portfolio Runs

Three separate local run directories contain 12 chapter files and a compiled `full.md` each. Their measured full-text lengths were approximately 30,187, 30,375, and 30,445 words. The fourth directory contains planning and story-bible artifacts only, with no generated chapter text. These runs are evidence of production output, not independent editorial approval or publication.

## Streaming Observation

One historical request that had failed near an upstream gateway's approximately 60-second timeout completed in approximately 125 seconds after streaming response handling was added. This confirms one request path worked past the prior limit; it is not a service-level objective or throughput guarantee.

## Verification Boundary

The current local working tree passes 375 regression tests. A separate five-area quality-closure report recorded offline and unit-test passes, while explicitly listing incomplete real end-to-end checks for story-bible generation, a new controlled three-chapter run, and a request exceeding the planned 90-second streaming threshold. Passing tests do not change the editorial assessment above.
