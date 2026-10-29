package org.json;

import java.util.LinkedHashMap;
import java.util.Map;

public class JSONObject {
    private final Map<String, Object> values = new LinkedHashMap<>();

    public JSONObject() {}

    public JSONObject(String json) {
        Parser parser = new Parser(json);
        parser.readObject(values);
    }

    public JSONObject put(String key, Object value) {
        values.put(key, value);
        return this;
    }

    public String optString(String key) {
        return optString(key, "");
    }

    public String optString(String key, String fallback) {
        Object value = values.get(key);
        return value == null ? fallback : String.valueOf(value);
    }

    public JSONArray optJSONArray(String key) {
        Object value = values.get(key);
        return value instanceof JSONArray ? (JSONArray) value : null;
    }

    public double optDouble(String key, double fallback) {
        Object value = values.get(key);
        if (value instanceof Number) return ((Number) value).doubleValue();
        if (value instanceof String) {
            try {
                return Double.parseDouble((String) value);
            } catch (NumberFormatException ignored) {
            }
        }
        return fallback;
    }

    @Override
    public String toString() {
        StringBuilder result = new StringBuilder("{");
        for (Map.Entry<String, Object> entry : values.entrySet()) {
            if (result.length() > 1) result.append(',');
            result.append(quote(entry.getKey())).append(':').append(valueToString(entry.getValue()));
        }
        return result.append('}').toString();
    }

    static String valueToString(Object value) {
        if (value == null) return "null";
        if (value instanceof Boolean || value instanceof Number || value instanceof JSONArray) return value.toString();
        return quote(String.valueOf(value));
    }

    private static String quote(String value) {
        StringBuilder result = new StringBuilder("\"");
        for (int i = 0; i < value.length(); i++) {
            char ch = value.charAt(i);
            switch (ch) {
                case '\\': result.append("\\\\"); break;
                case '\"': result.append("\\\""); break;
                case '\n': result.append("\\n"); break;
                case '\r': result.append("\\r"); break;
                case '\t': result.append("\\t"); break;
                default: result.append(ch);
            }
        }
        return result.append('\"').toString();
    }

    private static final class Parser {
        private final String text;
        private int index;

        Parser(String text) { this.text = text == null ? "" : text; }

        void readObject(Map<String, Object> target) {
            skipWhitespace();
            expect('{');
            skipWhitespace();
            while (peek() != '}') {
                String key = readString();
                skipWhitespace();
                expect(':');
                skipWhitespace();
                target.put(key, readValue());
                skipWhitespace();
                if (peek() == ',') {
                    index++;
                    skipWhitespace();
                } else {
                    break;
                }
            }
            expect('}');
            skipWhitespace();
            if (index != text.length()) throw new JSONException("trailing JSON content");
        }

        private Object readValue() {
            char ch = peek();
            if (ch == '\"') return readString();
            if (text.startsWith("true", index)) { index += 4; return Boolean.TRUE; }
            if (text.startsWith("false", index)) { index += 5; return Boolean.FALSE; }
            if (text.startsWith("null", index)) { index += 4; return null; }
            int start = index;
            while (index < text.length() && "0123456789-".indexOf(text.charAt(index)) >= 0) index++;
            if (start == index) throw new JSONException("unsupported JSON value");
            String number = text.substring(start, index);
            try {
                return Integer.parseInt(number);
            } catch (NumberFormatException error) {
                throw new JSONException("invalid JSON number " + number + ": " + error.getMessage());
            }
        }

        private String readString() {
            expect('\"');
            StringBuilder result = new StringBuilder();
            while (index < text.length()) {
                char ch = text.charAt(index++);
                if (ch == '\"') return result.toString();
                if (ch != '\\') {
                    result.append(ch);
                    continue;
                }
                if (index >= text.length()) throw new JSONException("unterminated escape");
                char escaped = text.charAt(index++);
                switch (escaped) {
                    case 'n': result.append('\n'); break;
                    case 'r': result.append('\r'); break;
                    case 't': result.append('\t'); break;
                    case '\\': result.append('\\'); break;
                    case '\"': result.append('\"'); break;
                    default: throw new JSONException("unsupported escape");
                }
            }
            throw new JSONException("unterminated string");
        }

        private char peek() {
            if (index >= text.length()) throw new JSONException("unexpected end of JSON");
            return text.charAt(index);
        }

        private void expect(char expected) {
            if (peek() != expected) throw new JSONException("expected " + expected);
            index++;
        }

        private void skipWhitespace() {
            while (index < text.length() && Character.isWhitespace(text.charAt(index))) index++;
        }
    }
}
