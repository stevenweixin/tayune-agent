<?php
/**
 * Plugin Name: TAYUNE Tea Companion
 * Description: A floating tea assistant with product and knowledge lookup. Ordinary search and checkout remain unchanged.
 * Version: 1.3.1
 * Requires PHP: 7.4
 */
if (!defined('ABSPATH')) { exit; }

add_action('wp_enqueue_scripts', function () {
    // 结账和账户页不挂载聊天，避免在填写个人/支付信息的页面增加干扰。
    if ((function_exists('is_checkout') && is_checkout()) ||
        (function_exists('is_account_page') && is_account_page())) { return; }
    $api_base = 'https://agent.tayunetea.com';
    $assets = plugin_dir_url(__FILE__) . 'assets/';
    wp_enqueue_script('tayune-tea-companion', $assets . 'tea_companion.js', array(), '1.3.1', true);
    // 安全序列化配置，避免手动拼接JS引号。此配置不含后台密钥。
    wp_add_inline_script('tayune-tea-companion', 'window.TAYUNE_TEA_COMPANION = ' . wp_json_encode(array(
        'apiBase' => untrailingslashit($api_base),
        // 低频公开试运行：密钥和日额度控制仍留在后端，不下发浏览器。
        'chatEnabled' => true,
        'homePage' => is_front_page(),
        'contactUrl' => home_url('/policies/shipping-returns/#contact-policy'),
        'privacyUrl' => home_url('/policies/privacy-policy/'),
        'shopUrl' => home_url('/shop/'),
    )) . ';', 'before');
});
