/*
 * SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: ESPRESSIF MIT
 */

#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_attr.h"
#include "esp_check.h"
#include "esp_ldo_regulator.h"
#include "esp_cam_ctlr.h"
#include "esp_cam_ctlr_csi.h"

#include "esp_video.h"
#include "esp_video_cam.h"
#include "esp_video_ioctl.h"
#include "esp_video_device_internal.h"
#include "esp_video_device_common.h"
#include "esp_video_csi_format.h"

/* Format mapping tables */
typedef struct {
    uint32_t v4l2_fmt;
    cam_ctlr_color_t csi_color;
    isp_color_t isp_color;
    uint8_t bpp;
} format_mapping_t;

static const char *TAG = "csi_format";

/* Forward declarations for helper functions */
static cam_ctlr_color_t v4l2_to_csi_color(uint32_t v4l2_fmt);
static isp_color_t v4l2_to_isp_color(uint32_t v4l2_fmt);
static bool is_isp_supported_input(uint32_t sensor_v4l2_fmt);
#if ESP_VIDEO_CSI_DEVICE_CONV_FORMAT
static bool is_csi_supported_input(uint32_t v4l2_fmt);
#endif
static bool is_csi_supported_output(uint32_t v4l2_fmt);
static bool is_isp_bypass_required(uint32_t sensor_v4l2_fmt, uint32_t requested_fmt);

/* V4L2 format to CSI format mapping */
static format_mapping_t v4l2_to_csi_mapping[] = {
    {V4L2_PIX_FMT_SBGGR8, CAM_CTLR_COLOR_RAW8, ISP_COLOR_RAW8, 8},
    {V4L2_PIX_FMT_SBGGR10, CAM_CTLR_COLOR_RAW10, ISP_COLOR_RAW10, 10},
    {V4L2_PIX_FMT_SBGGR12, CAM_CTLR_COLOR_RAW12, ISP_COLOR_RAW12, 12},
    {V4L2_PIX_FMT_RGB565, CAM_CTLR_COLOR_RGB565, ISP_COLOR_RGB565, 16},
    {V4L2_PIX_FMT_RGB565X, CAM_CTLR_COLOR_RGB565, ISP_COLOR_RGB565, 16}, // Big-endian handled separately
    {V4L2_PIX_FMT_RGB24, CAM_CTLR_COLOR_RGB888, ISP_COLOR_RGB888, 24},
    {V4L2_PIX_FMT_YUV420, CAM_CTLR_COLOR_YUV420, ISP_COLOR_YUV420, 12},
#if ESP_VIDEO_CSI_DEVICE_CONV_FORMAT
    {V4L2_PIX_FMT_UYVY, CAM_CTLR_COLOR_YUV422_UYVY, ISP_COLOR_YUV422, 16},
    {V4L2_PIX_FMT_VYUY, CAM_CTLR_COLOR_YUV422_VYUY, ISP_COLOR_YUV422, 16},
    {V4L2_PIX_FMT_YUYV, CAM_CTLR_COLOR_YUV422_YUYV, ISP_COLOR_YUV422, 16},
    {V4L2_PIX_FMT_YVYU, CAM_CTLR_COLOR_YUV422_YVYU, ISP_COLOR_YUV422, 16},
#else /* ESP_VIDEO_CSI_DEVICE_CONV_FORMAT */
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(6, 0, 0)
    {V4L2_PIX_FMT_UYVY, CAM_CTLR_COLOR_YUV422_UYVY, ISP_COLOR_YUV422, 16},
#else /* ESP_IDF_VERSION < ESP_IDF_VERSION_VAL(6, 0, 0) */
    {V4L2_PIX_FMT_UYVY, CAM_CTLR_COLOR_YUV422, ISP_COLOR_YUV422, 16},
#endif /* ESP_IDF_VERSION < ESP_IDF_VERSION_VAL(6, 0, 0) */
#endif /* ESP_VIDEO_CSI_DEVICE_CONV_FORMAT */
    {V4L2_PIX_FMT_GREY, CAM_CTLR_COLOR_GRAY8, ISP_COLOR_RAW8, 8},
};

#if ESP_VIDEO_CSI_DEVICE_CONV_FORMAT
/* Supported CSI input formats (from hardware spec) */
static uint32_t csi_supported_input_formats[] = {
    V4L2_PIX_FMT_RGB24,
    V4L2_PIX_FMT_RGB565,
    V4L2_PIX_FMT_YUV420,
    V4L2_PIX_FMT_YUYV,
    V4L2_PIX_FMT_VYUY,
    V4L2_PIX_FMT_YVYU,
    V4L2_PIX_FMT_UYVY,
    V4L2_PIX_FMT_SBGGR8,
    V4L2_PIX_FMT_SBGGR10,
    V4L2_PIX_FMT_SBGGR12,
    V4L2_PIX_FMT_GREY,
};

static uint32_t csi_supported_convert_formats[] = {
    V4L2_PIX_FMT_RGB24,
    V4L2_PIX_FMT_RGB565,
    V4L2_PIX_FMT_YUV420,
    V4L2_PIX_FMT_YUYV,
    V4L2_PIX_FMT_VYUY,
    V4L2_PIX_FMT_YVYU,
    V4L2_PIX_FMT_UYVY,
};
#endif

/* Supported CSI output formats (from hardware spec) */
static uint32_t csi_supported_output_formats[] = {
    V4L2_PIX_FMT_RGB565,
    V4L2_PIX_FMT_YUV420,
    V4L2_PIX_FMT_YUYV,
    V4L2_PIX_FMT_VYUY,
    V4L2_PIX_FMT_YVYU,
    V4L2_PIX_FMT_UYVY,
    V4L2_PIX_FMT_RGB24,
    V4L2_PIX_FMT_SBGGR8,
    V4L2_PIX_FMT_SBGGR10,
    V4L2_PIX_FMT_SBGGR12,
    V4L2_PIX_FMT_GREY,
};

/* ISP output formats (when not in bypass mode) */
static uint32_t isp_output_formats[] = {
    V4L2_PIX_FMT_SBGGR8,    // ISP_COLOR_RAW8
    V4L2_PIX_FMT_RGB565,    // ISP_COLOR_RGB565
    V4L2_PIX_FMT_RGB24,     // ISP_COLOR_RGB888
    V4L2_PIX_FMT_YUV420,    // ISP_COLOR_YUV420
    V4L2_PIX_FMT_UYVY,      // ISP_COLOR_YUV422(UYVY)
};

/* Sensor to V4L2 format mapping */
static uint32_t sensor_to_v4l2_format(esp_cam_sensor_output_format_t sensor_fmt)
{
    switch (sensor_fmt) {
    case ESP_CAM_SENSOR_PIXFORMAT_RGB565_LE:
        return V4L2_PIX_FMT_RGB565;
    case ESP_CAM_SENSOR_PIXFORMAT_RGB565_BE:
        return V4L2_PIX_FMT_RGB565X;
    case ESP_CAM_SENSOR_PIXFORMAT_YUV422_UYVY:
        return V4L2_PIX_FMT_UYVY;
    case ESP_CAM_SENSOR_PIXFORMAT_YUV422_YUYV:
        return V4L2_PIX_FMT_YUYV;
    case ESP_CAM_SENSOR_PIXFORMAT_YUV420:
        return V4L2_PIX_FMT_YUV420;
    case ESP_CAM_SENSOR_PIXFORMAT_RGB888:
        return V4L2_PIX_FMT_RGB24;
    case ESP_CAM_SENSOR_PIXFORMAT_RGB444:
    case ESP_CAM_SENSOR_PIXFORMAT_RGB555:
    case ESP_CAM_SENSOR_PIXFORMAT_BGR888:
        // These formats need conversion, not directly supported
        return 0;
    case ESP_CAM_SENSOR_PIXFORMAT_RAW8:
        return V4L2_PIX_FMT_SBGGR8;
    case ESP_CAM_SENSOR_PIXFORMAT_RAW10:
        return V4L2_PIX_FMT_SBGGR10;
    case ESP_CAM_SENSOR_PIXFORMAT_RAW12:
        return V4L2_PIX_FMT_SBGGR12;
    case ESP_CAM_SENSOR_PIXFORMAT_GRAYSCALE:
        return V4L2_PIX_FMT_GREY;
    default:
        break;
    }

    return 0; /* Default fallback */
}

/* Convert V4L2 format to ISP color format */
static isp_color_t v4l2_to_isp_color(uint32_t v4l2_fmt)
{
    for (size_t i = 0; i < sizeof(v4l2_to_csi_mapping) / sizeof(v4l2_to_csi_mapping[0]); i++) {
        if (v4l2_to_csi_mapping[i].v4l2_fmt == v4l2_fmt) {
            return v4l2_to_csi_mapping[i].isp_color;
        }
    }
    return ISP_COLOR_RGB888; /* Default fallback */
}

/* Convert V4L2 format to CSI color format */
static cam_ctlr_color_t v4l2_to_csi_color(uint32_t v4l2_fmt)
{
    for (size_t i = 0; i < sizeof(v4l2_to_csi_mapping) / sizeof(v4l2_to_csi_mapping[0]); i++) {
        if (v4l2_to_csi_mapping[i].v4l2_fmt == v4l2_fmt) {
            return v4l2_to_csi_mapping[i].csi_color;
        }
    }
    return CAM_CTLR_COLOR_RGB888; /* Default fallback */
}

/**
 * Check if ISP supports the sensor format as input
 */
static bool is_isp_supported_input(uint32_t sensor_v4l2_fmt)
{
    /* ISP supports RAW8 and RAW10 for processing */
    if (sensor_v4l2_fmt == V4L2_PIX_FMT_SBGGR8 ||
            sensor_v4l2_fmt == V4L2_PIX_FMT_SBGGR10) {
        return true;
    }

    /* Other formats require bypass mode */
    return false;
}

/**
 * Check if ISP bypass mode is required for the sensor format
 */
static bool is_isp_bypass_required(uint32_t sensor_v4l2_fmt, uint32_t requested_fmt)
{
    /* RAW formats require bypass mode */
    if (sensor_v4l2_fmt == requested_fmt) {
        return true;
    }

    /* ISP bypass is required for non-RAW formats and RAW12 */
    return !is_isp_supported_input(sensor_v4l2_fmt);
}

#if ESP_VIDEO_CSI_DEVICE_CONV_FORMAT
/**
 * Check if V4L2 format is supported by CSI as input
 */
static bool is_csi_supported_input(uint32_t v4l2_fmt)
{
    for (size_t i = 0; i < ARRAY_SIZE(csi_supported_input_formats); i++) {
        if (csi_supported_input_formats[i] == v4l2_fmt) {
            return true;
        }
    }

    return false;
}

static bool is_csi_supported_convert_input(uint32_t v4l2_fmt)
{
    for (size_t i = 0; i < ARRAY_SIZE(csi_supported_convert_formats); i++) {
        if (csi_supported_convert_formats[i] == v4l2_fmt) {
            return true;
        }
    }
    return false;
}
#endif

/**
 * Check if V4L2 format is supported by CSI as output
 */
static bool is_csi_supported_output(uint32_t v4l2_fmt)
{
    for (size_t i = 0; i < ARRAY_SIZE(csi_supported_output_formats); i++) {
        if (csi_supported_output_formats[i] == v4l2_fmt) {
            return true;
        }
    }

    return false;
}

static uint8_t isp_color_to_bpp(isp_color_t isp_color)
{
    for (size_t i = 0; i < ARRAY_SIZE(v4l2_to_csi_mapping); i++) {
        if (v4l2_to_csi_mapping[i].isp_color == isp_color) {
            return v4l2_to_csi_mapping[i].bpp;
        }
    }

    return 0;
}

/**
 * Enumerate all supported output formats for the CSI-ISP pipeline
 *
 * This function lists all possible output formats given the current
 * sensor output format and hardware capabilities.
 *
 * @param sensor_fmt Sensor output format
 * @param index Index of the format to enumerate
 * @param pixel_format Pointer to store the enumerated pixel format
 *
 * @return ESP_OK if format found, ESP_ERR_INVALID_ARG if index out of range
 */
esp_err_t esp_video_csi_enum_format(esp_cam_sensor_output_format_t sensor_fmt, uint32_t index, uint32_t *pixel_format)
{
    ESP_RETURN_ON_FALSE(pixel_format, ESP_ERR_INVALID_ARG, TAG, "pixel_format is NULL");

    uint32_t sensor_v4l2_fmt = sensor_to_v4l2_format(sensor_fmt);
    if (sensor_v4l2_fmt == 0) {
        ESP_LOGE(TAG, "Unsupported sensor format: %d", sensor_fmt);
        return ESP_ERR_NOT_SUPPORTED;
    }

    /**
     * Total supported formats are less than 16:
     * - ISP supported convert output: 1 + 5 = 6
     * - CSI supported convert output: 7
     * - Total: 13
     */
    uint32_t supported_v4l2_fmt[16];
    uint8_t supported_v4l2_fmt_count = 0;
    bool is_isp_enabled = is_isp_supported_input(sensor_v4l2_fmt);

    if (is_isp_enabled) {
        /**
         * If sensor input format is RAW10, we need to support to output it directly
         */
        if (sensor_v4l2_fmt == V4L2_PIX_FMT_SBGGR10) {
            supported_v4l2_fmt[supported_v4l2_fmt_count++] = sensor_v4l2_fmt;
        }

        for (int i = 0; i < ARRAY_SIZE(isp_output_formats); i++) {
            supported_v4l2_fmt[supported_v4l2_fmt_count++] = isp_output_formats[i];
        }
    } else {
        /**
         * ISP enables the bypass mode, so the ISP output format is the sensor input format
         */
        supported_v4l2_fmt[supported_v4l2_fmt_count++] = sensor_v4l2_fmt;
    }

#if ESP_VIDEO_CSI_DEVICE_CONV_FORMAT
    /**
     * CSI supports the format conversion, so we need to check if the format is supported by CSI
     */
    if (is_isp_enabled || is_csi_supported_convert_input(sensor_v4l2_fmt)) {
        int saved_fmt_count = supported_v4l2_fmt_count;
        bool strip_rgb888 = !is_isp_enabled && (sensor_v4l2_fmt != V4L2_PIX_FMT_RGB24);

        for (int i = 0; i < ARRAY_SIZE(csi_supported_convert_formats); i++) {
            if (strip_rgb888 && csi_supported_convert_formats[i] == V4L2_PIX_FMT_RGB24) {
                continue;
            }

            bool skip_format = false;
            for (int j = 0; j < saved_fmt_count; j++) {
                if (supported_v4l2_fmt[j] == csi_supported_convert_formats[i]) {
                    skip_format = true;
                    break;
                }
            }

            if (skip_format) {
                continue;
            }

            supported_v4l2_fmt[supported_v4l2_fmt_count++] = csi_supported_convert_formats[i];
        }
    }
#endif

#if 0
    /**
     * Log the supported formats for debugging
     */
    ESP_LOGI(TAG, "Supported formats: %d", supported_v4l2_fmt_count);
    for (int i = 0; i < supported_v4l2_fmt_count; i++) {
        ESP_LOGI(TAG, "  [%2d] %" PRIx32 " ("V4L2_FMT_STR")", i, supported_v4l2_fmt[i], V4L2_FMT_STR_ARG(supported_v4l2_fmt[i]));
    }
    ESP_LOGI(TAG, "--------------------------------");
#endif

    if (index >= supported_v4l2_fmt_count) {
        ESP_LOGD(TAG, "Format index %" PRIu32 " out of range (total %d)", index, supported_v4l2_fmt_count);
        return ESP_ERR_INVALID_ARG;
    }

    *pixel_format = supported_v4l2_fmt[index];
    ESP_LOGD(TAG, "Enumerated format[%" PRIu32 "]=%" PRIx32, index, *pixel_format);

    return ESP_OK;
}

/**
 * Check if the requested V4L2 format can be supported by the CSI-ISP pipeline
 *
 * This function validates if the requested output format can be produced
 * given the sensor's output format and the capabilities of ISP and MIPI-CSI.
 *
 * @param sensor_fmt Sensor output format
 * @param v4l2_fmt V4L2 format, if 0, use the sensor format
 * @param in_out_format Pointer to the CSI-ISP input/output format structure
 *
 * @return ESP_OK if format is supported, ESP_ERR_NOT_SUPPORTED otherwise
 */
esp_err_t esp_video_csi_check_format(esp_cam_sensor_output_format_t sensor_fmt, uint32_t v4l2_fmt, esp_video_csi_isp_in_out_format_t *in_out_format)
{
    ESP_RETURN_ON_FALSE(in_out_format, ESP_ERR_INVALID_ARG, TAG, "in_out_format is NULL");

    uint32_t requested_fmt = v4l2_fmt;
    uint32_t sensor_v4l2_fmt = sensor_to_v4l2_format(sensor_fmt);

    if (sensor_v4l2_fmt == 0) {
        ESP_LOGE(TAG, "Unsupported sensor format: %d", sensor_fmt);
        return ESP_ERR_NOT_SUPPORTED;
    }

    /**
     * If the requested format is not set, use the sensor format, so the ISP is in the bypass mode
     */
    if (requested_fmt == 0) {
        requested_fmt = sensor_v4l2_fmt;
    }

    ESP_LOGD(TAG, "Checking format: requested=%" PRIx32 ", sensor_fmt=%d (0x%" PRIx32 ")",
             requested_fmt, sensor_fmt, sensor_v4l2_fmt);

    /* Step 1: Check if requested format is supported by CSI output */
    bool csi_supports_output = is_csi_supported_output(requested_fmt);
    if (!csi_supports_output) {
        ESP_LOGD(TAG, "Requested format %" PRIx32 " not supported by CSI output", requested_fmt);
        return ESP_ERR_NOT_SUPPORTED;
    }

    /* Step 2: Determine ISP mode (bypass or processing) */
    bool isp_bypass_required = is_isp_bypass_required(sensor_v4l2_fmt, requested_fmt);

    if (isp_bypass_required) {
        /* ISP is in bypass mode - it passes through data without processing */
        ESP_LOGD(TAG, "ISP in bypass mode (sensor format requires it)");

        /* In bypass mode, ISP output = ISP input = sensor output */
        uint32_t isp_output_fmt = sensor_v4l2_fmt;

        in_out_format->isp_bypass_required = true;
        in_out_format->isp_input_fmt = v4l2_to_isp_color(sensor_v4l2_fmt);
        in_out_format->isp_output_fmt = in_out_format->isp_input_fmt; /* Bypass: input = output */
        in_out_format->isp_bpp = isp_color_to_bpp(in_out_format->isp_output_fmt);

#if ESP_VIDEO_CSI_DEVICE_CONV_FORMAT
        /* New chip: CSI can convert formats */
        /* Check if CSI supports conversion from ISP output to requested format */
        if (!is_csi_supported_input(isp_output_fmt)) {
            ESP_LOGD(TAG, "CSI doesn't support input format %" PRIx32, isp_output_fmt);
            return ESP_ERR_NOT_SUPPORTED;
        }

        in_out_format->csi_input_fmt = v4l2_to_csi_color(sensor_v4l2_fmt);
        in_out_format->csi_output_fmt = v4l2_to_csi_color(requested_fmt);

#if CONFIG_ESP_VIDEO_ENABLE_ISP_VIDEO_DEVICE
        if (sensor_v4l2_fmt == V4L2_PIX_FMT_SBGGR8 && requested_fmt == V4L2_PIX_FMT_SBGGR8 && !esp_video_isp_video_device_is_raw_bypass()) {
            ESP_LOGI(TAG, "ISP bypass mode is disabled by user");
            in_out_format->isp_bypass_required = false;
        }
#endif

        /* CSI supports the conversion */
        ESP_LOGD(TAG, "Format supported: sensor->ISP(bypass)->CSI(convert)->requested");
        return ESP_OK;
#else
        /* Old chip: CSI cannot convert formats */
        /* ISP output must match requested format */
        if (isp_output_fmt == requested_fmt) {
            in_out_format->csi_input_fmt = v4l2_to_csi_color(sensor_v4l2_fmt);
            in_out_format->csi_output_fmt = v4l2_to_csi_color(requested_fmt);

#if CONFIG_ESP_VIDEO_ENABLE_ISP_VIDEO_DEVICE
            if (sensor_v4l2_fmt == V4L2_PIX_FMT_SBGGR8 && requested_fmt == V4L2_PIX_FMT_SBGGR8 && !esp_video_isp_video_device_is_raw_bypass()) {
                ESP_LOGI(TAG, "ISP bypass mode is disabled by user");
                in_out_format->isp_bypass_required = false;
            }
#endif

            ESP_LOGD(TAG, "Format supported: sensor->ISP(bypass)->CSI(passthrough)");
            return ESP_OK;
        } else {
            ESP_LOGD(TAG, "Format mismatch: ISP output=%" PRIx32 ", requested=%" PRIx32,
                     isp_output_fmt, requested_fmt);
            return ESP_ERR_NOT_SUPPORTED;
        }
#endif /* ESP_VIDEO_CSI_DEVICE_CONV_FORMAT */

    } else {
        /* ISP can process the data (sensor outputs RAW format) */
        ESP_LOGD(TAG, "ISP can process RAW data");

        /* Check if ISP can output the requested format */
        bool isp_can_output = false;
        for (size_t i = 0; i < sizeof(isp_output_formats) / sizeof(isp_output_formats[0]); i++) {
            if (isp_output_formats[i] == requested_fmt) {
                isp_can_output = true;
                break;
            }
        }

        in_out_format->isp_bypass_required = false;
        in_out_format->isp_input_fmt = v4l2_to_isp_color(sensor_v4l2_fmt);

#if ESP_VIDEO_CSI_DEVICE_CONV_FORMAT
        if (!isp_can_output) {
            /* Use RGB888 because this format has more color details and it's supported by CSI */
            ESP_LOGD(TAG, "ISP cannot output requested format %" PRIx32 ", so ISP output RGB888 to CSI", requested_fmt);
            in_out_format->isp_output_fmt = ISP_COLOR_RGB888;
        } else {
            /* ISP can output the requested format */
            in_out_format->isp_output_fmt = v4l2_to_isp_color(requested_fmt);
        }

        /* New chip: CSI can convert, but we need to check if CSI supports ISP's output as input */
        uint32_t csi_input_fmt = isp_can_output ? requested_fmt : V4L2_PIX_FMT_RGB24;
        if (!is_csi_supported_input(csi_input_fmt)) {
            ESP_LOGD(TAG, "CSI doesn't support ISP output format %" PRIx32 " as input", csi_input_fmt);
            return ESP_ERR_NOT_SUPPORTED;
        }

        /* ISP output to CSI */
        in_out_format->csi_input_fmt = v4l2_to_csi_color(csi_input_fmt);
        in_out_format->csi_output_fmt = v4l2_to_csi_color(requested_fmt);

        ESP_LOGD(TAG, "Format supported: sensor->ISP(process)->CSI(convert/passthrough)->requested");
        return ESP_OK;
#else
        /* Old chip: CSI cannot convert, ISP output must match requested format */
        if (!isp_can_output) {
            ESP_LOGD(TAG, "ISP cannot output requested format %" PRIx32, requested_fmt);
            return ESP_ERR_NOT_SUPPORTED;
        }

        /* ISP output to requested format */
        in_out_format->isp_output_fmt = v4l2_to_isp_color(requested_fmt);

        /*
         * From ESP-IDF v5.5.6, input_data_color_type is the color entering CSI
         * (the ISP output), not the sensor format. Revisions below v3.0 cannot
         * convert, so the CSI input must match the ISP output.
         */
#if ESP_IDF_VERSION < ESP_IDF_VERSION_VAL(5, 5, 6)
        /* Old IDF version: use sensor format as CSI input */
        in_out_format->csi_input_fmt = v4l2_to_csi_color(sensor_v4l2_fmt);
#else
        in_out_format->csi_input_fmt = v4l2_to_csi_color(requested_fmt);
#endif
        in_out_format->csi_output_fmt = v4l2_to_csi_color(requested_fmt);

        ESP_LOGD(TAG, "Format supported: sensor->ISP(process)->CSI(passthrough)");
        return ESP_OK;
#endif /* ESP_VIDEO_CSI_DEVICE_CONV_FORMAT */
    }
}
