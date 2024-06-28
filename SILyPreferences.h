/*
SILy 
Copyright (C) 2024 Pierre Gaufillet

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
*/

#ifndef SILYPREFERENCES_H
#define SILYPREFERENCES_H

#include <Preferences.h>

class SILyPreferences {
    public:
        SILyPreferences();
        String get(const char * ns, const char * key);
        void put(const char * ns, const char * key, const char * value);
        String generateJson();
        bool parseJson(String json);
        bool init();
    
    private:
        Preferences *prefs;
};

#endif // SILYPREFERENCES_H
