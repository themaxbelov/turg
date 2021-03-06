import json
import attr
from aiohttp import web, WSMsgType
from aiohttp.web import WebSocketResponse

from turg.logger import getLogger
from turg.models import get_voxels, verify_payload, store_voxel, Voxel
from turg.firebase import get_user_color

logger = getLogger()


class WebSocket(web.View):
    async def get(self):
        try:
            uid = self.request.query['uid']
        except KeyError:
            return web.json_response(
                {'error': {'message': 'Data not valid'}}, status=400)

        try:
            color = await get_user_color(self.request.app, uid)
        except ValueError:
            return web.json_response(
                {'error': {'message': 'Can\'t get color info'}}, status=500)

        if not color:
            return web.json_response(status=401)

        logger.info('User color: %s', color)

        ws = WebSocketResponse()
        await ws.prepare(self.request)

        self.request.app['websockets'].append(ws)
        self.request.app['websockets_colors'][id(ws)] = color

        await ws.send_json({
            'data': {'color': color},
            'meta': {'type': 'userColor'},
        })

        ratelimiter = self.request.app['limiter']

        async for msg in ws:
            logger.info("MSG: %s", msg)
            if ratelimiter.limit_exceeded(uid):
                logger.error("Rate limit for user %s exceeded", uid)
                msg = f'Requests limit of {ratelimiter.requests} per minute exceeded'
                await ws.send_json({'error': {'message': msg}})
                continue
            if msg.tp == WSMsgType.text:
                if msg.data == 'close':
                    logger.info("Close ws connection")
                    await ws.close()
                else:
                    try:
                        data = json.loads(msg.data)
                    except:
                        pass
                    else:
                        logger.info("Got request: %s", data)
                        await process_request(data, ws, self.request.app)

            elif msg.tp == WSMsgType.error:
                logger.exception("Got ws error %s", id(ws))

        self.request.app['websockets'].remove(ws)

        return ws


def factory(app):
    return {
        'method': 'GET',
        'path': '/v1/ws/',
        'handler': WebSocket,
        'expect_handler': web.Request.json,
    }


async def process_request(data, ws, app):
    if not isinstance(data, dict) or 'type' not in data or 'args' not in data:
        return {'error': {'message': {'Method and args required'}}}

    _id = data.get('id', None)
    _type = data['type'].lower()
    args = data['args']
    meta = {'id': _id, 'type': _type}

    if _type == 'range':
        await retrieve(args, ws, app, meta)
    elif _type == 'update':
        await place(args, ws, app, meta)
    else:
        await ws.send_json({
            'error': {'message': {'Unknown method or no method specified'}},
            'meta': meta,
        })


async def retrieve(args, ws, app, meta):
    x, y, r = args.get('x', 0), args.get('y', 0), args.get('range', 25)
    voxels = await get_voxels(x, y, r, app['db'])
    await ws.send_json({'data': voxels, 'meta': meta})


async def place(args, ws, app, meta):
    args.pop('name', None)
    if not verify_payload(args):
        return await ws.send_json({
            'error': {'message': 'Invalid payload'},
            'meta': meta,
        })

    try:
        args['owner'] = app['websockets_colors'][id(ws)]
        logger.info('WS color: %s', args['owner'])
        voxel = await store_voxel(Voxel(**args), app['db'])
    except (ValueError, KeyError) as e:
        res = {
            'error': {'message': str(e)},
            'meta': meta,
        }
        if e.args and isinstance(e.args[0], dict):
            res['error'] = e.args[0]
        return await ws.send_json(res)
    else:
        return await broadcast(voxel, app, meta)


async def broadcast(data, app, meta):
    if not isinstance(data, dict):
        data = attr.asdict(data)
        data.pop('updated', None)
        if not data.get('name'):
            data.pop('name', None)

    for ws in app['websockets']:
        try:
            await ws.send_json({'data': data, 'meta': meta})
            logger.info("Broadcast data %s for %s", meta['id'], id(ws))
        except:
            logger.exception("Failed to send update to socket %s", id(ws))


def in_range(voxel, position):
    x, y, r = position.get('x', 0), position.get('y', 0), position.get('range', 25)
    x_in_range = x - r < voxel.x < x + r
    y_in_range = y - r < voxel.y < y + r

    return x_in_range and y_in_range
