/** Match the complete committed answer line, not a number inside a denial.
 *
 * A result such as "FINAL_ANSWER: 10% is not valid" must fail. This is a
 * deterministic exact-answer check; source relevance and units still require
 * separate review. Markdown emphasis is presentation, not part of the value.
 */
module.exports = (output, context) => {
  const record = typeof output === 'string' ? JSON.parse(output) : output;
  const lines = String(record.answer ?? '').replace(/\*\*/g, '').split(/\r?\n/);
  const committed = lines.map((line) => line.trim()).filter((line) => /^FINAL_ANSWER:/i.test(line));
  const expected = new RegExp(`^(?:${context.vars.answer_pattern})[.]?$`, 'i');
  const pass = record.status === 'ok' && committed.length === 1 && expected.test(committed[0]);
  return {
    pass,
    score: pass ? 1 : 0,
    reason: pass ? 'Complete final-answer line matches' : 'Missing, contradictory, or incorrect final-answer line',
  };
};
