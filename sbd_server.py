<?php
/**
 * Plugin Name: Bin Collection Portal
 * Description: Postcode and House Number search tool.
 * Version: 2.5 (UX Update)
 * Author: Gemini
 */

if (!defined('ABSPATH')) {
    exit;
}

// ---------------------------------------------------------
// 1. SETTINGS PAGE
// ---------------------------------------------------------
add_action('admin_menu', 'sbd_add_admin_menu');
add_action('admin_init', 'sbd_settings_init');

function sbd_add_admin_menu() {
    add_options_page('Bin Portal Settings', 'Bin Portal', 'manage_options', 'bin_portal', 'sbd_options_page');
}

function sbd_settings_init() {
    register_setting('sbdPlugin', 'sbd_api_url');
    register_setting('sbdPlugin', 'sbd_council_module');
    
    add_settings_section('sbd_plugin_page_section', 'API Configuration', null, 'bin_portal');
    add_settings_field('sbd_api_url', 'Python API URL', 'sbd_api_url_render', 'bin_portal', 'sbd_plugin_page_section');
    add_settings_field('sbd_council_module', 'Council Script', 'sbd_council_module_render', 'bin_portal', 'sbd_plugin_page_section');
}

function sbd_api_url_render() {
    $default_url = 'https://ukbincollectiondata.onrender.com';
    $value = get_option('sbd_api_url', $default_url);
    if(empty($value)) $value = $default_url;
    echo "<input type='text' name='sbd_api_url' id='sbd_api_input' value='" . esc_attr($value) . "' style='width: 100%; max-width: 400px;'>";
}

function sbd_council_module_render() {
    $current_val = get_option('sbd_council_module');
    ?>
    <div style="display:flex; gap:10px; align-items:center;">
        <select name="sbd_council_module" id="sbd_council_select" style="min-width:250px;">
            <option value="<?php echo esc_attr($current_val); ?>" selected><?php echo esc_html($current_val ? $current_val : 'Select Council...'); ?></option>
        </select>
        <button type="button" id="sbd_load_councils" class="button">Refresh List</button>
    </div>
    <script>
    document.getElementById('sbd_load_councils').addEventListener('click', function() {
        var btn = this;
        var select = document.getElementById('sbd_council_select');
        btn.innerText = "Loading...";
        
        var data = new FormData();
        data.append('action', 'sbd_fetch_councils_list');
        
        fetch('<?php echo admin_url('admin-ajax.php'); ?>', { method: 'POST', body: data })
        .then(r => r.json())
        .then(res => {
            btn.innerText = "Refresh List";
            if(res.success && res.data.councils) {
                select.innerHTML = '<option value="">-- Select Council --</option>';
                res.data.councils.forEach(c => {
                    var opt = document.createElement('option');
                    opt.value = c;
                    opt.innerText = c;
                    if(c === "<?php echo esc_js($current_val); ?>") opt.selected = true;
                    select.appendChild(opt);
                });
                alert("List updated!");
            } else {
                alert("Error fetching list.");
            }
        });
    });
    </script>
    <?php
}

function sbd_options_page() {
    ?>
    <div class="wrap">
        <h2>Bin Portal Settings</h2>
        <form action="options.php" method="post">
            <?php
            settings_fields('sbdPlugin');
            do_settings_sections('bin_portal');
            submit_button();
            ?>
        </form>
    </div>
    <?php
}

// ---------------------------------------------------------
// 2. SHORTCODE: [bin_finder]
// ---------------------------------------------------------
add_shortcode('bin_finder', 'sbd_bin_finder_shortcode');

function sbd_bin_finder_shortcode() {
    ob_start();
    ?>
    <div id="sbd-portal">
        <div id="sbd-search-box">
            <h3>Find Your Bin Schedule</h3>
            <p style="font-size:0.9em; color:#666; margin-bottom:10px;">Enter your Postcode, or House Number + Postcode.</p>
            <div style="display:flex; gap:10px; max-width:500px;">
                <!-- Updated Placeholder -->
                <input type="text" id="sbd-postcode" placeholder="e.g. SN8 1RA or 10 SN8 1RA" style="padding:10px; flex-grow:1;">
                <button onclick="sbdGetSchedule()" style="padding:10px; background:#0073aa; color:white; border:none; cursor:pointer;">Search</button>
            </div>
            <div id="sbd-error" style="color:red; margin-top:10px;"></div>
        </div>

        <div id="sbd-results-area" style="display:none; margin-top:20px;">
            <h3>Your Collection Dates</h3>
            <div id="sbd-results" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 15px; margin-bottom: 20px;"></div>
            
            <div style="background:#f9f9f9; padding:15px; border-radius:5px; border:1px solid #eee;">
                <strong>Subscribe:</strong>
                <a id="sbd-ics-link" href="#" class="button">📅 ICS / Outlook</a>
                <a id="sbd-gcal-link" href="#" target="_blank" class="button">G+ Google Calendar</a>
            </div>
            <br>
            <button onclick="location.reload()" style="font-size:0.8em; cursor:pointer;">&larr; Search Again</button>
        </div>
    </div>

    <script>
    function sbdGetSchedule() {
        const pc = document.getElementById('sbd-postcode').value;
        const container = document.getElementById('sbd-results');
        const errorDiv = document.getElementById('sbd-error');
        const resultsArea = document.getElementById('sbd-results-area');
        
        if(!pc) { errorDiv.innerText = "Please enter a postcode."; return; }
        
        errorDiv.innerText = "Searching... (This can take 10-20 seconds)";
        container.innerHTML = "Loading...";
        
        const data = new FormData();
        data.append('action', 'sbd_proxy_bins');
        data.append('address_data', pc); 

        fetch('<?php echo admin_url('admin-ajax.php'); ?>', { method: 'POST', body: data })
        .then(r => r.json())
        .then(res => {
            if(!res.success) {
                // Better error handling for empty results
                if (res.data && res.data.includes('Script failed')) {
                     errorDiv.innerText = "Error: " + res.data.replace('Script failed: ', '');
                } else {
                     errorDiv.innerText = "Error: " + (res.data || "Unknown server error");
                }
                container.innerHTML = "";
                return;
            }
            
            const bins = res.data.bins || res.data;

            if (!bins || !Array.isArray(bins)) {
                 errorDiv.innerText = "No bin data found. Try adding your house number (e.g. '10 SN8 1RA').";
                 container.innerHTML = "";
                 return;
            }

            if (bins.length === 0) {
                 errorDiv.innerText = "Council found the address but returned no dates. Please try again later.";
                 container.innerHTML = "";
                 return;
            }

            // Success - Show Results
            errorDiv.innerText = "";
            container.innerHTML = "";
            resultsArea.style.display = 'block';

            bins.forEach(bin => {
                const div = document.createElement('div');
                div.style = "border:1px solid #ccc; padding:15px; border-radius:5px; background:#fff; text-align:center;";
                div.innerHTML = `<strong style='display:block; margin-bottom:5px;'>${bin.type}</strong><span style='font-size:1.2em; color:#0073aa;'>${bin.collectionDate}</span>`;
                container.appendChild(div);
            });

            const siteUrl = "<?php echo home_url('/'); ?>";
            const feedUrl = siteUrl + "?sbd_feed=ics&uprn=" + encodeURIComponent(pc);
            const webcalUrl = feedUrl.replace("https://", "webcal://").replace("http://", "webcal://");
            
            document.getElementById('sbd-ics-link').href = webcalUrl;
            document.getElementById('sbd-gcal-link').href = "https://www.google.com/calendar/render?cid=" + encodeURIComponent(feedUrl);
        })
        .catch(e => {
            errorDiv.innerText = "System Error. Please try again.";
        });
    }
    </script>
    <?php
    return ob_get_clean();
}

// ---------------------------------------------------------
// 3. AJAX HANDLERS
// ---------------------------------------------------------
function sbd_get_url() {
    $url = get_option('sbd_api_url');
    if(empty($url)) $url = 'https://ukbincollectiondata.onrender.com';
    return rtrim($url, '/');
}

add_action('wp_ajax_sbd_fetch_councils_list', 'sbd_fetch_councils_list');
function sbd_fetch_councils_list() {
    $api_url = sbd_get_url();
    $response = wp_remote_get("$api_url/get_councils", ['timeout' => 15]);
    if (is_wp_error($response)) wp_send_json_error();
    $data = json_decode(wp_remote_retrieve_body($response), true);
    wp_send_json_success($data);
}

add_action('wp_ajax_sbd_proxy_bins', 'sbd_ajax_bins');
add_action('wp_ajax_nopriv_sbd_proxy_bins', 'sbd_ajax_bins');

function sbd_ajax_bins() {
    $api_url = sbd_get_url();
    $module = get_option('sbd_council_module');
    $addr = $_POST['address_data']; 

    $response = wp_remote_post("$api_url/get_bins", [
        'body' => json_encode(['address_data' => $addr, 'module' => $module]),
        'headers' => ['Content-Type' => 'application/json'],
        'timeout' => 120
    ]);

    if (is_wp_error($response)) wp_send_json_error($response->get_error_message());
    $data = json_decode(wp_remote_retrieve_body($response), true);
    if (isset($data['error'])) wp_send_json_error($data['error']);
    if (isset($data['detail'])) wp_send_json_error($data['detail']);
    wp_send_json_success($data);
}

// ---------------------------------------------------------
// 4. ICS FEED
// ---------------------------------------------------------
add_action('init', 'sbd_check_ics_feed');
function sbd_check_ics_feed() {
    if (isset($_GET['sbd_feed']) && $_GET['sbd_feed'] == 'ics' && isset($_GET['uprn'])) {
        $uprn = $_GET['uprn'];
        $api_url = sbd_get_url();
        $module = get_option('sbd_council_module');

        $response = wp_remote_post("$api_url/get_bins", [
            'body' => json_encode(['address_data' => $uprn, 'module' => $module]),
            'headers' => ['Content-Type' => 'application/json'],
            'timeout' => 120
        ]);

        if (is_wp_error($response)) die("Error");
        $json = json_decode(wp_remote_retrieve_body($response), true);
        $bins = isset($json['bins']) ? $json['bins'] : [];

        header('Content-Type: text/calendar; charset=utf-8');
        header('Content-Disposition: attachment; filename="bins.ics"');
        echo "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Bin Portal//EN\r\n";
        foreach($bins as $bin) {
            $type = $bin['type'];
            $dateObj = DateTime::createFromFormat('d/m/Y', $bin['collectionDate']);
            if (!$dateObj) continue;
            $dateStr = $dateObj->format('Ymd');
            $uid = md5($type . $dateStr) . "@binportal";
            echo "BEGIN:VEVENT\r\nUID:$uid\r\nDTSTART;VALUE=DATE:$dateStr\r\nSUMMARY:Bin: $type\r\nEND:VEVENT\r\n";
        }
        echo "END:VCALENDAR";
        exit;
    }
}
