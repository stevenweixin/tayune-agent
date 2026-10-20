<?php
/**
 * Plugin Name: TAYUNE Search Assistant Reference
 * Description: Add Ask AI to the existing search dialog. Reference, not installed.
 * Version: 0.2.0
 */
// WordPress integration reference: never embed API keys. Install only after endpoint verification.
if (!defined('ABSPATH')) { exit; }

add_action('wp_enqueue_scripts', function () {
    // 填入审核过的固定HTTPS后端地址，不填API密钥；留空时不注入AI入口。
    $api_base = '';
    $parts = wp_parse_url($api_base);
    if (!$parts || ($parts['scheme'] ?? '') !== 'https' || empty($parts['host'])) { return; }
    $assets = plugin_dir_url(__FILE__) . 'assets/';
    wp_enqueue_style('tayune-search-assistant', $assets . 'search_assistant.css', array(), '0.2.0');
    wp_enqueue_script('tayune-search-assistant', $assets . 'search_assistant.js', array(), '0.2.0', true);
    // 安全序列化配置，避免手动拼接JS引号。此配置不含后台密钥。
    wp_add_inline_script('tayune-search-assistant', 'window.TAYUNE_SEARCH_ASSISTANT = ' . wp_json_encode(array(
        'apiBase' => untrailingslashit($api_base),
        'contactUrl' => home_url('/policies/shipping-returns/#contact-policy'),
    )) . ';', 'before');
});
