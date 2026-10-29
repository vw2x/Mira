package com.vwww.mira.relay;

import java.net.URISyntaxException;

public final class RelayEndpointTest {
    private interface ThrowingCall {
        String run() throws Exception;
    }

    private static void equal(String expected, String actual) {
        if (!expected.equals(actual)) {
            throw new AssertionError("expected <" + expected + "> but was <" + actual + ">");
        }
    }

    private static void fails(Class<? extends Throwable> type, ThrowingCall call) throws Exception {
        try {
            call.run();
        } catch (Throwable failure) {
            if (type.isInstance(failure)) return;
            throw new AssertionError("expected " + type.getName() + " but caught " + failure, failure);
        }
        throw new AssertionError("expected " + type.getName());
    }

    private static void endpoints(String input, String control, String screen) throws Exception {
        equal(control, RelayEndpoint.controlWebSocket(input));
        equal(screen, RelayEndpoint.screenWebSocket(input));
    }

    public static void main(String[] args) throws Exception {
        String[][] normalizedCases = new String[][] {
            {null, ""},
            {" \t\n", ""},
            {"relay.example:8765", "https://relay.example:8765"},
            {" relay.example/base/ ", "https://relay.example/base/"},
            {" HTTP://Relay.EXAMPLE/base?x=1#part ", "HTTP://Relay.EXAMPLE/base?x=1#part"},
            {"ws://relay.example/ws/control", "ws://relay.example/ws/control"},
            {":// malformed but normalization does not parse ", ":// malformed but normalization does not parse"}
        };
        for (String[] golden : normalizedCases) {
            equal(golden[1], RelayEndpoint.normalizeBaseUrl(golden[0]));
        }

        String[][] endpointCases = new String[][] {
            {"relay.example", "wss://relay.example/ws/control", "wss://relay.example/ws/screen/device"},
            {" http://relay.example:8765/ ", "ws://relay.example:8765/ws/control", "ws://relay.example:8765/ws/screen/device"},
            {"https://relay.example", "wss://relay.example/ws/control", "wss://relay.example/ws/screen/device"},
            {"ws://relay.example/base", "ws://relay.example/base/ws/control", "ws://relay.example/base/ws/screen/device"},
            {"WSS://Relay.EXAMPLE/base/", "wss://Relay.EXAMPLE/base/ws/control", "wss://Relay.EXAMPLE/base/ws/screen/device"},
            {"https://relay.example/base///", "wss://relay.example/base/ws/control", "wss://relay.example/base/ws/screen/device"},
            {"https://relay.example/path%20segment", "wss://relay.example/path%20segment/ws/control", "wss://relay.example/path%20segment/ws/screen/device"},
            {"https://[2001:db8::1]:9443/base", "wss://[2001:db8::1]:9443/base/ws/control", "wss://[2001:db8::1]:9443/base/ws/screen/device"},
            {"https://relay.example/base?token=abc#fragment", "wss://relay.example/base/ws/control", "wss://relay.example/base/ws/screen/device"},
            {"https://relay.example/?token=abc", "wss://relay.example/ws/control", "wss://relay.example/ws/screen/device"},
            {"https://relay.example/ws/control?token=abc", "wss://relay.example/ws/control", "wss://relay.example/ws/control/ws/screen/device"},
            {"https://relay.example/ws/screen/device#fragment", "wss://relay.example/ws/screen/device/ws/control", "wss://relay.example/ws/screen/device"},
            {"https://relay.example/ws/control/", "wss://relay.example/ws/control/ws/control", "wss://relay.example/ws/control/ws/screen/device"},
            {"ws:/base", "wss://ws:/base/ws/control", "wss://ws:/base/ws/screen/device"}
        };
        for (String[] golden : endpointCases) {
            endpoints(golden[0], golden[1], golden[2]);
        }

        for (String empty : new String[] {null, "", " \n "}) {
            fails(IllegalArgumentException.class, () -> RelayEndpoint.controlWebSocket(empty));
            fails(IllegalArgumentException.class, () -> RelayEndpoint.screenWebSocket(empty));
        }
        for (String unsupported : new String[] {"ftp://relay.example", "file:///tmp/relay"}) {
            fails(IllegalArgumentException.class, () -> RelayEndpoint.controlWebSocket(unsupported));
            fails(IllegalArgumentException.class, () -> RelayEndpoint.screenWebSocket(unsupported));
        }
        for (String missingHost : new String[] {"https:///base"}) {
            fails(IllegalArgumentException.class, () -> RelayEndpoint.controlWebSocket(missingHost));
            fails(IllegalArgumentException.class, () -> RelayEndpoint.screenWebSocket(missingHost));
        }
        for (String malformed : new String[] {"://relay.example", "https://relay.example/%", "https://[2001:db8::1"}) {
            fails(URISyntaxException.class, () -> RelayEndpoint.controlWebSocket(malformed));
            fails(URISyntaxException.class, () -> RelayEndpoint.screenWebSocket(malformed));
        }

        System.out.println("PASS: relay URL normalization and endpoint golden cases");
    }
}
