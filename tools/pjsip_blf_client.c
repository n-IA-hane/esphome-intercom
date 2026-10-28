/* Independent RFC 4235 subscriber using unmodified PJSIP 2.16.
 * Build with tools/build_pjsip_blf.py. All addresses are loopback-only.
 * stdout is JSONL; PJSIP diagnostics go to stderr.
 */
#include <pjsip.h>
#include <pjsip_ua.h>
#include <pjsip_simple.h>
#include <pjlib-util.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static pjsip_endpoint *endpoint;
static pjsip_evsub *subscription;
static pjsip_regc *registration;
static pjsip_tpselector selector;
static pj_str_t local_uri, contact_uri, target_uri;
static int reject_next_notify;

static void json_string(const char *value, size_t length)
{
    size_t i;
    putchar('"');
    for (i = 0; i < length; ++i) {
        unsigned char ch = (unsigned char)value[i];
        if (ch == '"' || ch == '\\') printf("\\%c", ch);
        else if (ch < 32) printf("\\u%04x", ch);
        else putchar(ch);
    }
    putchar('"');
}

static void pj_json_string(pj_str_t value)
{
    json_string(value.ptr, (size_t)value.slen);
}

static void check(pj_status_t status, const char *operation)
{
    if (status != PJ_SUCCESS) {
        char error[PJ_ERR_MSG_SIZE];
        pj_strerror(status, error, sizeof(error));
        fprintf(stderr, "%s: %s (%d)\n", operation, error, status);
        exit(2);
    }
}

static void logger(int level, const char *data, int length)
{
    PJ_UNUSED_ARG(level);
    fwrite(data, 1, (size_t)length, stderr);
}

static void on_registration(struct pjsip_regc_cbparam *param)
{
    printf("{\"event\":\"registration\",\"code\":%d,\"status\":%d,\"unregister\":%s}\n",
           param->code, param->status, param->is_unreg ? "true" : "false");
}

static void on_state(pjsip_evsub *sub, pjsip_event *event)
{
    PJ_UNUSED_ARG(event);
    printf("{\"event\":\"subscription\",\"state\":\"%s\"}\n", pjsip_evsub_get_state_name(sub));
    if (pjsip_evsub_get_state(sub) == PJSIP_EVSUB_STATE_TERMINATED)
        subscription = NULL;
}

static void on_transaction(pjsip_evsub *sub, pjsip_transaction *tsx, pjsip_event *event)
{
    PJ_UNUSED_ARG(sub);
    if (event->type == PJSIP_EVENT_TSX_STATE &&
        event->body.tsx_state.type == PJSIP_EVENT_RX_MSG &&
        tsx->role == PJSIP_ROLE_UAC && tsx->status_code >= 200) {
        printf("{\"event\":\"response\",\"code\":%d,\"cseq\":%d,\"method\":",
               tsx->status_code, tsx->cseq);
        pj_json_string(tsx->method.name);
        puts("}");
    }
}

static void on_notify(pjsip_evsub *sub, pjsip_rx_data *rdata, int *code,
                      pj_str_t **text, pjsip_hdr *headers, pjsip_msg_body **body)
{
    pjsip_dlg_event_status state;
    pjsip_msg *message = rdata->msg_info.msg;
    pj_str_t state_name = pj_str("Subscription-State");
    pjsip_hdr *header = pjsip_msg_find_hdr_by_name(message, &state_name, NULL);
    char printed[1024];
    int length = header ? pjsip_hdr_print_on(header, printed, sizeof(printed)) : 0;
    PJ_UNUSED_ARG(text); PJ_UNUSED_ARG(headers); PJ_UNUSED_ARG(body);
    check(pjsip_dlg_event_get_status(sub, &state), "parse dialog event");
    *code = reject_next_notify ? 481 : 200;
    reject_next_notify = 0;
    printf("{\"event\":\"notify\",\"response\":%d,\"cseq\":%d,\"subscription_state\":",
           *code, rdata->msg_info.cseq->cseq);
    json_string(printed, length > 0 ? (size_t)length : 0);
    printf(",\"xml\":");
    json_string(message->body ? message->body->data : "",
                message->body ? message->body->len : 0);
    printf(",\"parsed_count\":%u,\"parsed_state\":", state.info_cnt);
    if (state.info_cnt) pj_json_string(state.info[0].dialog_state); else json_string("", 0);
    printf(",\"parsed_call_id\":");
    if (state.info_cnt) pj_json_string(state.info[0].dialog_call_id); else json_string("", 0);
    puts("}");
}

static void no_automatic_refresh(pjsip_evsub *sub)
{
    PJ_UNUSED_ARG(sub);
    puts("{\"event\":\"refresh_due\"}");
}

static void send_subscription(int expires)
{
    pjsip_tx_data *request;
    if (!subscription) {
        pjsip_dialog *dialog;
        pjsip_evsub_user callbacks;
        pj_bzero(&callbacks, sizeof(callbacks));
        callbacks.on_evsub_state = on_state;
        callbacks.on_tsx_state = on_transaction;
        callbacks.on_rx_notify = on_notify;
        callbacks.on_client_refresh = no_automatic_refresh;
        check(pjsip_dlg_create_uac(pjsip_ua_instance(), &local_uri, &contact_uri,
                                  &target_uri, NULL, &dialog), "create dialog");
        /* The new dialog has zero usages. Match pjsua_pres.c ownership while
         * configuring it, before the event package acquires its usage. */
        pjsip_dlg_inc_lock(dialog);
        check(pjsip_dlg_set_transport(dialog, &selector), "set dialog transport");
        check(pjsip_dlg_event_create_uac(dialog, &callbacks, PJSIP_EVSUB_NO_EVENT_ID,
                                       &subscription), "create subscription");
        pjsip_dlg_dec_lock(dialog);
    }
    check(pjsip_dlg_event_initiate(subscription, expires, &request), "create SUBSCRIBE");
    check(pjsip_dlg_event_send_request(subscription, request), "send SUBSCRIBE");
}

int main(int argc, char **argv)
{
    pj_caching_pool cache;
    pj_sockaddr_in address;
    pj_str_t loopback = pj_str("127.0.0.1");
    pjsip_transport *udp = NULL;
    pjsip_tpfactory *tcp = NULL;
    pjsip_cred_info credentials;
    pjsip_tx_data *request;
    char local[256], contact[256], target[256], server[256], command[128];
    unsigned port;
    int tcp_mode, exiting = 0;
    if (argc != 4 || strcmp(argv[1], "127.0.0.1") ||
        (strcmp(argv[3], "UDP") && strcmp(argv[3], "TCP"))) {
        fprintf(stderr, "usage: %s 127.0.0.1 SERVER_PORT UDP|TCP\n", argv[0]);
        return 2;
    }
    tcp_mode = !strcmp(argv[3], "TCP");
    setvbuf(stdout, NULL, _IOLBF, 0);
    pj_log_set_log_func(logger);
    pj_log_set_level(2);
    check(pj_init(), "pj_init");
    check(pjlib_util_init(), "pjlib_util_init");
    pj_caching_pool_init(&cache, NULL, 0);
    check(pjsip_endpt_create(&cache.factory, "blf-qualification", &endpoint), "create endpoint");
    check(pjsip_tsx_layer_init_module(endpoint), "transaction module");
    check(pjsip_ua_init_module(endpoint, NULL), "dialog module");
    check(pjsip_evsub_init_module(endpoint), "event module");
    check(pjsip_dlg_event_init_module(endpoint, pjsip_evsub_instance()), "dialog event module");
    pj_sockaddr_in_init(&address, &loopback, 0);
    pj_bzero(&selector, sizeof(selector));
    if (tcp_mode) {
        check(pjsip_tcp_transport_start(endpoint, &address, 1, &tcp), "TCP transport");
        selector.type = PJSIP_TPSELECTOR_LISTENER;
        selector.u.listener = tcp;
        port = (unsigned)tcp->addr_name.port;
    } else {
        check(pjsip_udp_transport_start(endpoint, &address, NULL, 1, &udp), "UDP transport");
        selector.type = PJSIP_TPSELECTOR_TRANSPORT;
        selector.u.transport = udp;
        port = (unsigned)udp->local_name.port;
    }
    snprintf(server, sizeof(server), "sip:127.0.0.1:%s;transport=%s", argv[2], argv[3]);
    snprintf(local, sizeof(local), "sip:blfwatch@127.0.0.1:%s", argv[2]);
    snprintf(contact, sizeof(contact), "sip:blfwatch@127.0.0.1:%u;transport=%s", port, argv[3]);
    snprintf(target, sizeof(target), "sip:101@127.0.0.1:%s;transport=%s", argv[2], argv[3]);
    local_uri = pj_str(local); contact_uri = pj_str(contact); target_uri = pj_str(target);
    check(pjsip_regc_create(endpoint, NULL, on_registration, &registration), "registration client");
    {
        pj_str_t server_uri = pj_str(server);
        check(pjsip_regc_init(registration, &server_uri, &local_uri, &local_uri, 1,
                              &contact_uri, 300), "registration configuration");
    }
    pj_bzero(&credentials, sizeof(credentials));
    credentials.realm = pj_str("*"); credentials.scheme = pj_str("digest");
    credentials.username = pj_str("blfwatch");
    credentials.data_type = PJSIP_CRED_DATA_PLAIN_PASSWD;
    credentials.data = pj_str("isolated-test-only");
    check(pjsip_regc_set_credentials(registration, 1, &credentials), "registration credentials");
    check(pjsip_regc_set_transport(registration, &selector), "registration transport");
    check(pjsip_regc_register(registration, PJ_FALSE, &request), "create REGISTER");
    check(pjsip_regc_send(registration, request), "send REGISTER");
    fcntl(STDIN_FILENO, F_SETFL, fcntl(STDIN_FILENO, F_GETFL) | O_NONBLOCK);
    while (!exiting) {
        pj_time_val timeout = {0, 20};
        pjsip_endpt_handle_events(endpoint, &timeout);
        clearerr(stdin);
        while (fgets(command, sizeof(command), stdin)) {
            int expires;
            if (sscanf(command, "subscribe %d", &expires) == 1 && expires >= 0)
                send_subscription(expires);
            else if (!strncmp(command, "reject481", 9)) {
                reject_next_notify = 1;
                puts("{\"event\":\"reject_ready\"}");
            } else if (!strncmp(command, "unregister", 10)) {
                check(pjsip_regc_unregister(registration, &request), "create unregister");
                check(pjsip_regc_send(registration, request), "send unregister");
            } else if (!strncmp(command, "quit", 4)) exiting = 1;
            else { fprintf(stderr, "unknown command: %s", command); return 2; }
        }
    }
    if (subscription) pjsip_dlg_event_terminate(subscription, PJ_FALSE);
    pjsip_regc_destroy(registration);
    pjsip_endpt_destroy(endpoint);
    pj_caching_pool_destroy(&cache);
    pj_shutdown();
    return 0;
}
