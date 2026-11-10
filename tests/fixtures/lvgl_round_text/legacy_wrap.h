#pragma once
#include "lvgl.h"
#include <array>
#include <string>
#include <vector>

// Extracted from round_runtime.yaml at snapshot 0326342f. Only globals and
// setters are replaced by returned rows so the old algorithm is an oracle.
struct LegacyText {
  std::vector<std::string> rows;
  size_t candidate_bytes{0}, measurements{0};
};
template<typename Widths> LegacyText legacy_wrap(const std::string &text, const Widths &chords,
                                                const lv_font_t *font, bool thinking) {
  LegacyText result;
  if (thinking) result.rows.resize(10);
  if (thinking && text == "...") return result;
  std::vector<std::string> words;
  words.reserve(text.size() / 5);
  size_t start = 0;
  for (size_t i = 0; i <= text.size(); ++i) {
    if (i == text.size() || text[i] == ' ' || text[i] == '\n' || text[i] == '\r' || text[i] == '\t') {
      if (i > start) words.push_back(text.substr(start, i - start));
      start = i + 1;
    }
  }
  if (words.empty() || chords.empty()) return result;
  const int avg_char_w = lv_font_get_line_height(font) * 2 / 3;
  size_t first=chords.size()/2-3;
  if (first >= chords.size()) first=0;
  size_t word_idx = 0, safety = 0, line_count = thinking ? first : 0;
  while (word_idx < words.size() &&
         (thinking ? line_count < chords.size() && line_count < 10 : safety++ < words.size() * 2)) {
    const int chord_w = chords[line_count % chords.size()];
    if (chord_w <= 0) {
      if (!thinking) result.rows.push_back("");
      ++line_count; continue;
    }
    const size_t start_word = word_idx;
    std::string line;
    line.reserve(chord_w / avg_char_w);
    while (word_idx < words.size()) {
      const auto &word = words[word_idx];
      std::string candidate = line.empty() ? word : (line + ' ' + word);
      result.candidate_bytes += candidate.size();
      if ((int) (candidate.length() * avg_char_w) > chord_w) {
        lv_point_t size;
        lv_text_get_size(&size, candidate.c_str(), font, 0, 0, INT32_MAX, LV_TEXT_FLAG_NONE);
        ++result.measurements;
        if (size.x > chord_w) {
          if (word_idx == start_word) { line = word; ++word_idx; }
          break;
        }
      }
      line = candidate;
      ++word_idx;
    }
    if (word_idx > start_word) {
      if (thinking) result.rows[line_count] = line;
      else result.rows.push_back(line);
      ++line_count;
    }
  }
  if (!thinking) while (!result.rows.empty() && result.rows.size() % 10 != 0) result.rows.push_back("");
  return result;
}
