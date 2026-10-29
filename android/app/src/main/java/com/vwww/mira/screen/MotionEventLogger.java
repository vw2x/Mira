package com.vwww.mira.screen;

import com.vwww.mira.BuildConfig;

import android.os.Build;
import android.util.Log;
import android.view.InputDevice;
import android.view.MotionEvent;

import org.json.JSONArray;
import org.json.JSONObject;

import java.lang.reflect.Field;
import java.lang.reflect.Modifier;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.TreeMap;
import java.util.concurrent.atomic.AtomicLong;

public final class MotionEventLogger {
    private static final String TAG = "MiraMotionEvent";
    private static final int LOG_CHUNK_SIZE = 3500;
    private static final AtomicLong NEXT_SEQUENCE = new AtomicLong();
    private static final TreeMap<Integer, String> AXIS_NAMES = discoverMotionEventConstants("AXIS_");
    private static final TreeMap<Integer, String> CLASSIFICATION_NAMES = discoverMotionEventConstants("CLASSIFICATION_");
    private static final TreeMap<Integer, String> SOURCE_NAMES = discoverInputDeviceConstants("SOURCE_");

    private MotionEventLogger() {
    }

    public static void log(MotionEvent event) {
        if (!BuildConfig.DEBUG || event == null) return;
        try {
            long sequence = NEXT_SEQUENCE.incrementAndGet();
            emit(buildEventRecord(sequence, event));
            emitPointerRecords(sequence, event);
            emitHistoryRecords(sequence, event);
        } catch (Throwable throwable) {
            Log.w(TAG, "MotionEvent logging failed", throwable);
        }
    }

    private static JSONObject buildEventRecord(long sequence, MotionEvent event) {
        JSONObject json = baseRecord(sequence, "event");
        put(json, "motionEventString", event::toString);
        put(json, "action", event::getAction);
        put(json, "actionName", () -> safeActionName(event.getAction()));
        put(json, "actionMasked", event::getActionMasked);
        put(json, "actionMaskedName", () -> safeActionName(event.getActionMasked()));
        put(json, "actionIndex", event::getActionIndex);
        put(json, "pointerCount", event::getPointerCount);
        put(json, "historySize", event::getHistorySize);
        put(json, "downTime", event::getDownTime);
        put(json, "eventTime", event::getEventTime);
        put(json, "eventAgeMs", () -> System.currentTimeMillis() - event.getEventTime());
        put(json, "deviceId", event::getDeviceId);
        put(json, "device", () -> describeDevice(event.getDevice()));
        put(json, "source", event::getSource);
        put(json, "sourceName", () -> sourceName(event.getSource()));
        put(json, "flags", event::getFlags);
        put(json, "edgeFlags", event::getEdgeFlags);
        put(json, "metaState", event::getMetaState);
        put(json, "buttonState", event::getButtonState);
        put(json, "xPrecision", event::getXPrecision);
        put(json, "yPrecision", event::getYPrecision);
        put(json, "rawX", event::getRawX);
        put(json, "rawY", event::getRawY);
        put(json, "orientation", event::getOrientation);
        put(json, "pressure", event::getPressure);
        put(json, "size", event::getSize);
        put(json, "touchMajor", event::getTouchMajor);
        put(json, "touchMinor", event::getTouchMinor);
        put(json, "toolMajor", event::getToolMajor);
        put(json, "toolMinor", event::getToolMinor);
        put(json, "classification", () -> Build.VERSION.SDK_INT >= 29 ? event.getClassification() : JSONObject.NULL);
        put(json, "classificationName", () -> Build.VERSION.SDK_INT >= 29 ? classificationName(event.getClassification()) : JSONObject.NULL);
        put(json, "displayId", () -> reflectiveCall(event, "getDisplayId"));
        put(json, "axisValues", () -> axisValues(event, -1, -1));
        return json;
    }

    private static void emitPointerRecords(long sequence, MotionEvent event) {
        int pointerCount = safeInt(event::getPointerCount, 0);
        for (int pointerIndex = 0; pointerIndex < pointerCount; pointerIndex++) {
            final int index = pointerIndex;
            JSONObject json = baseRecord(sequence, "pointer");
            put(json, "pointerIndex", () -> index);
            put(json, "pointerId", () -> event.getPointerId(index));
            put(json, "toolType", () -> event.getToolType(index));
            put(json, "toolTypeName", () -> toolTypeName(event.getToolType(index)));
            put(json, "x", () -> event.getX(index));
            put(json, "y", () -> event.getY(index));
            put(json, "rawX", () -> rawX(event, index));
            put(json, "rawY", () -> rawY(event, index));
            put(json, "orientation", () -> event.getOrientation(index));
            put(json, "pressure", () -> event.getPressure(index));
            put(json, "size", () -> event.getSize(index));
            put(json, "touchMajor", () -> event.getTouchMajor(index));
            put(json, "touchMinor", () -> event.getTouchMinor(index));
            put(json, "toolMajor", () -> event.getToolMajor(index));
            put(json, "toolMinor", () -> event.getToolMinor(index));
            put(json, "pointerProperties", () -> pointerProperties(event, index));
            put(json, "pointerCoords", () -> pointerCoords(event, index, -1));
            put(json, "axisValues", () -> axisValues(event, index, -1));
            emit(json);
        }
    }

    private static void emitHistoryRecords(long sequence, MotionEvent event) {
        int historySize = safeInt(event::getHistorySize, 0);
        int pointerCount = safeInt(event::getPointerCount, 0);
        for (int historyIndex = 0; historyIndex < historySize; historyIndex++) {
            final int h = historyIndex;
            JSONObject sample = baseRecord(sequence, "history");
            put(sample, "historyIndex", () -> h);
            put(sample, "historicalEventTime", () -> event.getHistoricalEventTime(h));
            JSONArray pointers = new JSONArray();
            for (int pointerIndex = 0; pointerIndex < pointerCount; pointerIndex++) {
                final int p = pointerIndex;
                JSONObject pointer = new JSONObject();
                put(pointer, "pointerIndex", () -> p);
                put(pointer, "pointerId", () -> event.getPointerId(p));
                put(pointer, "x", () -> event.getHistoricalX(p, h));
                put(pointer, "y", () -> event.getHistoricalY(p, h));
                put(pointer, "orientation", () -> event.getHistoricalOrientation(p, h));
                put(pointer, "pressure", () -> event.getHistoricalPressure(p, h));
                put(pointer, "size", () -> event.getHistoricalSize(p, h));
                put(pointer, "touchMajor", () -> event.getHistoricalTouchMajor(p, h));
                put(pointer, "touchMinor", () -> event.getHistoricalTouchMinor(p, h));
                put(pointer, "toolMajor", () -> event.getHistoricalToolMajor(p, h));
                put(pointer, "toolMinor", () -> event.getHistoricalToolMinor(p, h));
                put(pointer, "pointerCoords", () -> pointerCoords(event, p, h));
                put(pointer, "axisValues", () -> axisValues(event, p, h));
                pointers.put(pointer);
            }
            safeJsonPut(sample, "pointers", pointers);
            emit(sample);
        }
    }

    private static JSONObject pointerProperties(MotionEvent event, int pointerIndex) {
        JSONObject json = new JSONObject();
        MotionEvent.PointerProperties properties = new MotionEvent.PointerProperties();
        put(json, "load", () -> {
            event.getPointerProperties(pointerIndex, properties);
            return "ok";
        });
        put(json, "id", () -> properties.id);
        put(json, "toolType", () -> properties.toolType);
        put(json, "toolTypeName", () -> toolTypeName(properties.toolType));
        return json;
    }

    private static JSONObject pointerCoords(MotionEvent event, int pointerIndex, int historyIndex) {
        JSONObject json = new JSONObject();
        MotionEvent.PointerCoords coords = new MotionEvent.PointerCoords();
        put(json, "load", () -> {
            if (historyIndex >= 0) event.getHistoricalPointerCoords(pointerIndex, historyIndex, coords);
            else event.getPointerCoords(pointerIndex, coords);
            return "ok";
        });
        put(json, "x", () -> coords.x);
        put(json, "y", () -> coords.y);
        put(json, "pressure", () -> coords.pressure);
        put(json, "size", () -> coords.size);
        put(json, "touchMajor", () -> coords.touchMajor);
        put(json, "touchMinor", () -> coords.touchMinor);
        put(json, "toolMajor", () -> coords.toolMajor);
        put(json, "toolMinor", () -> coords.toolMinor);
        put(json, "orientation", () -> coords.orientation);
        put(json, "axisValues", () -> axisValues(coords, event));
        return json;
    }

    private static JSONObject axisValues(MotionEvent event, int pointerIndex, int historyIndex) {
        JSONObject json = new JSONObject();
        for (int axis : axesFor(event)) {
            final int currentAxis = axis;
            String key = axisName(currentAxis);
            if (historyIndex >= 0) {
                put(json, key, () -> event.getHistoricalAxisValue(currentAxis, pointerIndex, historyIndex));
            } else if (pointerIndex >= 0) {
                put(json, key, () -> event.getAxisValue(currentAxis, pointerIndex));
            } else {
                put(json, key, () -> event.getAxisValue(currentAxis));
            }
        }
        return json;
    }

    private static JSONObject axisValues(MotionEvent.PointerCoords coords, MotionEvent event) {
        JSONObject json = new JSONObject();
        for (int axis : axesFor(event)) {
            final int currentAxis = axis;
            put(json, axisName(currentAxis), () -> coords.getAxisValue(currentAxis));
        }
        return json;
    }

    private static List<Integer> axesFor(MotionEvent event) {
        Set<Integer> axes = new LinkedHashSet<>();
        axes.add(MotionEvent.AXIS_X);
        axes.add(MotionEvent.AXIS_Y);
        axes.add(MotionEvent.AXIS_PRESSURE);
        axes.add(MotionEvent.AXIS_SIZE);
        axes.add(MotionEvent.AXIS_TOUCH_MAJOR);
        axes.add(MotionEvent.AXIS_TOUCH_MINOR);
        axes.add(MotionEvent.AXIS_TOOL_MAJOR);
        axes.add(MotionEvent.AXIS_TOOL_MINOR);
        axes.add(MotionEvent.AXIS_ORIENTATION);
        axes.add(MotionEvent.AXIS_VSCROLL);
        axes.add(MotionEvent.AXIS_HSCROLL);
        axes.add(MotionEvent.AXIS_DISTANCE);
        axes.add(MotionEvent.AXIS_TILT);
        try {
            InputDevice device = event.getDevice();
            if (device != null) {
                for (InputDevice.MotionRange range : device.getMotionRanges()) {
                    if (range != null) axes.add(range.getAxis());
                }
            }
        } catch (Throwable ignored) {
        }
        return new ArrayList<>(axes);
    }

    private static JSONObject describeDevice(InputDevice device) {
        if (device == null) return null;
        JSONObject json = new JSONObject();
        put(json, "id", device::getId);
        put(json, "name", device::getName);
        put(json, "descriptor", device::getDescriptor);
        put(json, "sources", device::getSources);
        put(json, "sourcesName", () -> sourceName(device.getSources()));
        put(json, "keyboardType", device::getKeyboardType);
        put(json, "vendorId", () -> Build.VERSION.SDK_INT >= 19 ? device.getVendorId() : JSONObject.NULL);
        put(json, "productId", () -> Build.VERSION.SDK_INT >= 19 ? device.getProductId() : JSONObject.NULL);
        JSONArray ranges = new JSONArray();
        try {
            for (InputDevice.MotionRange range : device.getMotionRanges()) {
                JSONObject item = new JSONObject();
                put(item, "axis", range::getAxis);
                put(item, "axisName", () -> axisName(range.getAxis()));
                put(item, "source", range::getSource);
                put(item, "sourceName", () -> sourceName(range.getSource()));
                put(item, "min", range::getMin);
                put(item, "max", range::getMax);
                put(item, "range", range::getRange);
                put(item, "flat", range::getFlat);
                put(item, "fuzz", range::getFuzz);
                put(item, "resolution", range::getResolution);
                ranges.put(item);
            }
        } catch (Throwable throwable) {
            ranges.put(errorObject(throwable));
        }
        safeJsonPut(json, "motionRanges", ranges);
        return json;
    }

    private static float rawX(MotionEvent event, int pointerIndex) {
        if (Build.VERSION.SDK_INT >= 29) return event.getRawX(pointerIndex);
        return event.getRawX() + event.getX(pointerIndex) - event.getX();
    }

    private static float rawY(MotionEvent event, int pointerIndex) {
        if (Build.VERSION.SDK_INT >= 29) return event.getRawY(pointerIndex);
        return event.getRawY() + event.getY(pointerIndex) - event.getY();
    }

    private static JSONObject baseRecord(long sequence, String recordType) {
        JSONObject json = new JSONObject();
        safeJsonPut(json, "type", "mira_motion_event");
        safeJsonPut(json, "record", recordType);
        safeJsonPut(json, "seq", sequence);
        safeJsonPut(json, "thread", Thread.currentThread().getName());
        safeJsonPut(json, "sdk", Build.VERSION.SDK_INT);
        safeJsonPut(json, "loggedAtMs", System.currentTimeMillis());
        return json;
    }

    private static String axisName(int axis) {
        String name = AXIS_NAMES.get(axis);
        if (name != null) return name;
        try {
            return MotionEvent.axisToString(axis);
        } catch (Throwable throwable) {
            return "AXIS_" + axis;
        }
    }

    private static String safeActionName(int action) {
        try {
            return MotionEvent.actionToString(action);
        } catch (Throwable throwable) {
            return "ACTION_" + action;
        }
    }

    private static String toolTypeName(int toolType) {
        switch (toolType) {
            case MotionEvent.TOOL_TYPE_FINGER:
                return "finger";
            case MotionEvent.TOOL_TYPE_STYLUS:
                return "stylus";
            case MotionEvent.TOOL_TYPE_MOUSE:
                return "mouse";
            case MotionEvent.TOOL_TYPE_ERASER:
                return "eraser";
            case MotionEvent.TOOL_TYPE_UNKNOWN:
            default:
                return "unknown(" + toolType + ")";
        }
    }

    private static String classificationName(int classification) {
        String name = CLASSIFICATION_NAMES.get(classification);
        return name == null ? "CLASSIFICATION_" + classification : name;
    }

    private static String sourceName(int source) {
        List<String> names = new ArrayList<>();
        for (Integer value : SOURCE_NAMES.keySet()) {
            if (value != null && value != 0 && (source & value) == value) {
                names.add(SOURCE_NAMES.get(value));
            }
        }
        return names.isEmpty() ? "SOURCE_0x" + Integer.toHexString(source) : names.toString();
    }

    private static Object reflectiveCall(Object target, String methodName) throws Throwable {
        try {
            return target.getClass().getMethod(methodName).invoke(target);
        } catch (NoSuchMethodException ignored) {
            return JSONObject.NULL;
        }
    }

    private static TreeMap<Integer, String> discoverMotionEventConstants(String prefix) {
        return discoverIntConstants(MotionEvent.class, prefix);
    }

    private static TreeMap<Integer, String> discoverInputDeviceConstants(String prefix) {
        return discoverIntConstants(InputDevice.class, prefix);
    }

    private static TreeMap<Integer, String> discoverIntConstants(Class<?> type, String prefix) {
        TreeMap<Integer, String> constants = new TreeMap<>();
        try {
            for (Field field : type.getFields()) {
                int modifiers = field.getModifiers();
                if (!Modifier.isStatic(modifiers) || field.getType() != int.class) continue;
                String name = field.getName();
                if (!name.startsWith(prefix)) continue;
                constants.put(field.getInt(null), name);
            }
        } catch (Throwable ignored) {
        }
        return constants;
    }

    private static void put(JSONObject object, String name, ValueReader reader) {
        try {
            Object value = reader.read();
            safeJsonPut(object, name, value == null ? JSONObject.NULL : normalizeValue(value));
        } catch (Throwable throwable) {
            safeJsonPut(object, name + "Error", shortError(throwable));
        }
    }

    private static Object normalizeValue(Object value) {
        if (value instanceof Float) return formatFloat((Float) value);
        if (value instanceof Double) return formatDouble((Double) value);
        return value;
    }

    private static String formatFloat(float value) {
        if (Float.isNaN(value) || Float.isInfinite(value)) return String.valueOf(value);
        return String.format(Locale.US, "%.4f", value);
    }

    private static String formatDouble(double value) {
        if (Double.isNaN(value) || Double.isInfinite(value)) return String.valueOf(value);
        return String.format(Locale.US, "%.4f", value);
    }

    private static void safeJsonPut(JSONObject object, String name, Object value) {
        try {
            object.put(name, value == null ? JSONObject.NULL : value);
        } catch (Throwable ignored) {
        }
    }

    private static JSONObject errorObject(Throwable throwable) {
        JSONObject json = new JSONObject();
        safeJsonPut(json, "error", shortError(throwable));
        return json;
    }

    private static String shortError(Throwable throwable) {
        if (throwable == null) return "unknown";
        String message = throwable.getMessage();
        String type = throwable.getClass().getSimpleName();
        return message == null || message.isEmpty() ? type : type + ": " + message;
    }

    private static int safeInt(IntReader reader, int fallback) {
        try {
            return reader.read();
        } catch (Throwable ignored) {
            return fallback;
        }
    }

    private static void emit(JSONObject json) {
        try {
            String line = json.toString();
            if (line.length() <= LOG_CHUNK_SIZE) {
                Log.i(TAG, line);
                return;
            }
            int chunkCount = (line.length() + LOG_CHUNK_SIZE - 1) / LOG_CHUNK_SIZE;
            Object sequence = json.opt("seq");
            Object record = json.opt("record");
            for (int index = 0; index < chunkCount; index++) {
                int start = index * LOG_CHUNK_SIZE;
                int end = Math.min(line.length(), start + LOG_CHUNK_SIZE);
                JSONObject chunk = new JSONObject();
                safeJsonPut(chunk, "type", "mira_motion_event_chunk");
                safeJsonPut(chunk, "seq", sequence == null ? JSONObject.NULL : sequence);
                safeJsonPut(chunk, "record", record == null ? JSONObject.NULL : record);
                safeJsonPut(chunk, "chunkIndex", index);
                safeJsonPut(chunk, "chunkCount", chunkCount);
                safeJsonPut(chunk, "payload", line.substring(start, end));
                Log.i(TAG, chunk.toString());
            }
        } catch (Throwable throwable) {
            Log.w(TAG, "MotionEvent emit failed", throwable);
        }
    }

    private interface ValueReader {
        Object read() throws Throwable;
    }

    private interface IntReader {
        int read() throws Throwable;
    }
}
