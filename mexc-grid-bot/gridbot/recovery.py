def client_id(order):
    return order.get('clientOrderId') or (order.get('info') or {}).get('clientOrderId') or (order.get('info') or {}).get('clientOrderID')
def reconcile(local_orders,exchange_orders):
    by_id={str(o.get('id')):o for o in exchange_orders if o.get('id') is not None}
    by_client={str(client_id(o)):o for o in exchange_orders if client_id(o)}
    result=[]
    for local in local_orders:
        remote=by_id.get(str(local['exchange_order_id'])) if local.get('exchange_order_id') else None
        remote=remote or by_client.get(str(local['client_order_id']))
        result.append({'local':dict(local),'exchange':remote,'action':'matched' if remote else 'verify_order_history_before_retry'})
    return result
