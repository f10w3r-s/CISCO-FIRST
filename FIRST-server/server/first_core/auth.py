#-------------------------------------------------------------------------------
#
#   FIRST Authentication module
#   Copyright (C) 2016  Angel M. Villegas
#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation; either version 2 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License along
#   with this program; if not, write to the Free Software Foundation, Inc.,
#   51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
#   Requirements
#   ------------
#   -   mongoengine
#
#-------------------------------------------------------------------------------

#   Python Modules
import os
import uuid
import json
import random
import datetime
from functools import wraps

#   Django Modules
from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponse, HttpRequest
from django.shortcuts import render, redirect
from django.urls import reverse

#   FIRST Modules
#   TODO: Use DBManager to get user objects and do User operations
from first.settings import CONFIG
from first_core.models import User
from first_core.error import FIRSTError

#   Thirdy Party
import httplib2
from oauth2client import client
from apiclient import discovery



class FIRSTAuthError(FIRSTError):
    _type_name = 'FIRSTAuth'

    def __init__(self, message):
        super(FIRSTError, self).__init__(message)


def verify_api_key(api_key):
    users = User.objects.filter(api_key=api_key)
    if not users:
        return None

    return users.get()


def require_apikey(view_function):
    @wraps(view_function)
    def decorated_function(*args, **kwargs):
        http401 = HttpResponse('Unauthorized', status=401)
        if 'api_key' not in kwargs:
            return http401

        key = kwargs['api_key'].lower()
        if key:
            user = verify_api_key(key)
            del kwargs['api_key']
            if user and user.active:
                kwargs['user'] = user
                return view_function(*args, **kwargs)

        return http401

    return decorated_function


def require_login(view_function):
    @wraps(view_function)
    def decorated_function(*args, **kwargs):
        request = None
        for arg in args:
            if isinstance(arg, HttpRequest):
                request = arg
                break

        if not request:
            return redirect(reverse('www:login'))

        auth = Authentication(request)
        if auth.is_logged_in:
            return view_function(*args, **kwargs)

        else:
            return redirect(reverse('www:login'))

    return decorated_function


class Authentication():
    '''
    self.request.session['auth'] = {'expires' : time token expires,
                                    'api_key' : uuid from users db

    }

    '''

    def __init__(self, request):
        self.request = request
        redirect_uri = request.build_absolute_uri(reverse('www:oauth', kwargs={'service' : 'google'}))
        secret = CONFIG.get('oauth_path', '/usr/local/etc/google_secret.json')
        try:
            self.flow = {'google' : client.flow_from_clientsecrets(secret,
                                    scope=['https://www.googleapis.com/auth/userinfo.profile',
                                            'https://www.googleapis.com/auth/userinfo.email'],
                                    redirect_uri=redirect_uri),
                }
        except TypeError as e:
            print(e)

        if 'auth' not in request.session:
            request.session['auth'] = {}


    @property
    def is_logged_in(self):
        if (('auth' not in self.request.session)
            or ('expires' not in self.request.session['auth'])):
            return False

        expires = datetime.datetime.fromtimestamp(self.request.session['auth']['expires'])
        if expires < datetime.datetime.now():
            return False

        return True


    def login_step_1(self, service, url):
        '''
        Called when user attempts to login with a service

        Function redirects to service to allow user to login, then redirects
        to the specified url
        '''
        if 'google' == service:
            self.request.session['auth']['service'] = 'google'
            auth_uri = self.flow['google'].step1_get_authorize_url()
            return redirect(auth_uri)

        raise FIRSTAuthError('Authentication service is not supported')


    def login_step_2(self, auth_code, url, login=True):
        '''
        Called when a service returns after a user logs in
        '''
        if (('auth' not in self.request.session)
            or ('service' not in self.request.session['auth'])):
            raise FIRSTAuthError('Authentication service is not supported')

        service = self.request.session['auth']['service']
        if 'google' == service:
            credentials = self.flow['google'].step2_exchange(auth_code).to_json()
            self.request.session['creds'] = credentials

            oauth = client.OAuth2Credentials.from_json(credentials)
            credentials = json.loads(credentials)

            if not oauth.access_token_expired:
                http_auth = oauth.authorize(httplib2.Http())
                service = discovery.build('oauth2', 'v2', http_auth)
                response = service.userinfo().v2().me().get().execute()
                self.request.session['info'] = {'name' : response['name'],
                                                'email' : response['email']}

                expires = credentials['id_token']['exp']
                #expires = datetime.datetime.fromtimestamp(expires)
                self.request.session['auth']['expires'] = expires

                if login:
                    try:
                        user = User.objects.get(email=self.request.session['info']['email'])
                        user.auth_data = json.dumps(credentials)
                        user.name = self.request.session['info']['name']
                        user.save()

                        self.request.session['auth']['api_key'] = str(user.api_key)

                        return redirect(url)

                    except ObjectDoesNotExist:
                        self.request.session.flush()
                        raise FIRSTAuthError('User is not registered.')

            return redirect(url)

        return redirect('www:login')

    def register_user(self):
        request = self.request
        required = ['handle', 'auth', 'info', 'creds']
        if False in [x in request.session for x in required]:
            return None

        if False in [x in request.session['info'] for x in ['email', 'name']]:
            return None

        if 'service' not in request.session['auth']:
            return None

        user = None
        handle = request.session['handle']
        service = request.session['auth']['service']
        name = request.session['info']['name']
        email = request.session['info']['email']
        credentials = request.session['creds']

        while not user:
            api_key = uuid.uuid4()

            #   Test if UUID exists
            try:
                user = User.objects.get(api_key=api_key)
                user = None
                continue

            except ObjectDoesNotExist:
                pass

            #   Create random 4 digit value for the handle
            #   This prevents handle collisions
            number = random.randint(0, 9999)
            for i in range(10000):
                try:
                    num = (number + i) % 10000
                    user = User.objects.get(handle=handle, number=num)
                    user = None

                except ObjectDoesNotExist:
                    user = User(name=name,
                                email=email,
                                api_key=api_key,
                                handle=handle,
                                number=num,
                                service=service,
                                auth_data=credentials)

                    user.save()
                    return user

        raise FIRSTAuthError('Unable to register user')


    @staticmethod
    def get_user_data(email):
        try:
            user = User.objects.get(email=email)
            return user

        except ObjectDoesNotExist:
            return None
