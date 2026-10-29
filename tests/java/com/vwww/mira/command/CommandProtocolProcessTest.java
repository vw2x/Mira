package com.vwww.mira.command;

import org.json.JSONObject;
import org.json.JSONException;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.DataInputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;

public final class CommandProtocolProcessTest {
    public static void main(String[] args) throws Exception {
        testLengthPrefixedUtf8Json();
        testOutOfRangeJsonNumber();
        testTextBase64AndResultFields();
        testResultNormalization();
        testProcessOutputAndExitCode();
        System.out.println("PASS: command framing, text encoding, result schema, and process output");
    }

    private static void testOutOfRangeJsonNumber() {
        try {
            new JSONObject("{\"value\":999999999999999999999}");
            throw new AssertionError("out-of-range JSON integer is rejected");
        } catch (JSONException expected) {
            // Expected.
        }
    }

    private static void testLengthPrefixedUtf8Json() throws Exception {
        JSONObject request = new JSONObject().put("tool", "mira-logcat").put("marker", "你好🙂");
        ByteArrayOutputStream wire = new ByteArrayOutputStream();
        LocalCommandProtocol.writeJson(wire, request);
        byte[] frame = wire.toByteArray();
        DataInputStream data = new DataInputStream(new ByteArrayInputStream(frame));
        int declaredLength = data.readInt();
        assertEquals(frame.length - 4, declaredLength, "frame length counts UTF-8 bytes");

        JSONObject decoded = LocalCommandProtocol.readJson(new ByteArrayInputStream(frame));
        assertEquals("mira-logcat", decoded.optString("tool"), "tool survives JSON frame");
        assertEquals("你好🙂", decoded.optString("marker"), "UTF-8 survives JSON frame");

        assertThrowsIOException(new byte[]{0, 0, 0, 0}, "zero frame length is rejected");
        assertThrowsIOException(new byte[]{0, 64, 0, 1}, "oversized frame is rejected");
    }

    private static void testTextBase64AndResultFields() throws Exception {
        String unicode = "line one\n零字节? no\n";
        assertEquals(unicode, LocalCommandProtocol.decode(LocalCommandProtocol.encode(unicode)),
            "text argument Base64 round trip");

        CommandResult result = new CommandResult(7, "标准输出\n", "warning\n");
        JSONObject json = LocalCommandProtocol.resultJson(result);
        assertEquals("1", json.optString("protocol"), "JSON protocol version");
        assertEquals("7", json.optString("exitCode"), "JSON exit code");
        assertEquals("标准输出\n", json.optString("stdout"), "JSON stdout");
        assertEquals("warning\n", json.optString("stderr"), "JSON stderr");

        ByteArrayOutputStream output = new ByteArrayOutputStream();
        LocalCommandProtocol.writeTextResult(output, result);
        String text = new String(output.toByteArray(), StandardCharsets.UTF_8);
        String expected = "MIRA/1 EXIT 7\n" +
            "STDOUT " + LocalCommandProtocol.encode("标准输出\n") + "\n" +
            "STDERR " + LocalCommandProtocol.encode("warning\n") + "\n" +
            "END\n";
        assertEquals(expected, text, "text response wire format");
    }

    private static void testResultNormalization() {
        CommandResult result = new CommandResult(-9, null, null);
        assertEquals(1, result.exitCode, "invalid exit code is normalized");
        assertEquals("", result.stdout, "null stdout is normalized");
        assertEquals("", result.stderr, "null stderr is normalized");
    }

    private static void testProcessOutputAndExitCode() {
        CommandResult result = ProcessCommandRunner.run(Arrays.asList(
            "/bin/sh", "-c", "printf 'host-out'; printf 'host-err' >&2; exit 7"
        ), 5_000L);
        assertEquals(7, result.exitCode, "host process exit code");
        assertEquals("host-out", result.stdout, "host process stdout");
        assertEquals("host-err", result.stderr, "host process stderr");

        CommandResult empty = ProcessCommandRunner.run(java.util.Collections.emptyList(), 5_000L);
        assertEquals(1, empty.exitCode, "empty argv fails");
        assertEquals("empty command\n", empty.stderr, "empty argv error");
    }

    private static void assertThrowsIOException(byte[] frame, String message) throws Exception {
        try {
            LocalCommandProtocol.readJson(new ByteArrayInputStream(frame));
            throw new AssertionError(message + ": expected IOException");
        } catch (IOException expected) {
            // Expected.
        }
    }

    private static void assertEquals(Object expected, Object actual, String message) {
        if (expected == null ? actual != null : !expected.equals(actual)) {
            throw new AssertionError(message + ": expected=" + expected + " actual=" + actual);
        }
    }
}
